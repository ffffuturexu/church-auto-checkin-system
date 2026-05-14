from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import Select, func, not_, or_, select
from sqlalchemy.orm import Session
from fastapi.responses import StreamingResponse

from app.core.database import get_db
from app.core.service_event import (
    get_or_create_sunday_service_event,
    infer_sunday_service_language,
    is_sunday,
    sunday_service_event_name,
    sunday_service_title,
)
from app.core.time_utils import now_local, now_local_naive, to_local_aware, today_local
from app.models.models import (
    AttendanceEvent,
    AttendanceRecord,
    CheckInMethod,
    Member,
    UnknownCaseStatus,
    UnknownFaceCase,
)
from app.services.reception_feed_service import record_feed_event
from app.schemas.attendance import (
    AttendanceDashboardDailyPoint,
    AttendanceDashboardEventPoint,
    AttendanceDashboardResponse,
    AttendanceDashboardSummary,
    AttendanceHistoryResponse,
    AttendanceRecordDeleteResponse,
    AttendanceRecordOut,
    CurrentServiceInfoResponse,
    ManualCheckInRequest,
    ManualCheckInResponse,
)

router = APIRouter(prefix="/attendance", tags=["attendance"])


def _event_language_filters():
    lowered_name = func.lower(AttendanceEvent.event_name)
    zh_filter = or_(
        AttendanceEvent.event_name.like("%中文%"),
        AttendanceEvent.event_name.like("%华语%"),
        lowered_name.like("%chinese%"),
        lowered_name.like("%mandarin%"),
    )
    it_filter = or_(
        AttendanceEvent.event_name.like("%意语%"),
        AttendanceEvent.event_name.like("%意大利%"),
        lowered_name.like("%italian%"),
        lowered_name.like("% ita%"),
        lowered_name.like("%ita%"),
    )
    return zh_filter, it_filter


def _to_display_checkin_time(value: datetime) -> datetime:
    return to_local_aware(value)


def _latest_sunday(ref_date: date) -> date:
    # Monday=0 ... Sunday=6
    days_since_sunday = (ref_date.weekday() + 1) % 7
    return ref_date - timedelta(days=days_since_sunday)


def _calc_this_year_avg_attendance_by_language(db: Session, today: date, language: str) -> float:
    current_year_start = date(today.year, 1, 1)
    zh_filter, it_filter = _event_language_filters()

    if language == "zh":
        # Unlabeled events are treated as Chinese by default to avoid dropping legacy data.
        language_filter = or_(zh_filter, not_(it_filter))
    elif language == "it":
        language_filter = it_filter
    else:
        return 0.0

    rows = db.execute(
        select(AttendanceEvent.id, func.count(AttendanceRecord.id))
        .outerjoin(AttendanceRecord, AttendanceRecord.event_id == AttendanceEvent.id)
        .where(
            AttendanceEvent.event_date >= current_year_start,
            AttendanceEvent.event_date <= today,
            func.strftime("%w", AttendanceEvent.event_date) == "0",
            language_filter,
        )
        .group_by(AttendanceEvent.id)
    ).all()
    if not rows:
        return 0.0

    total = sum(int(row[1] or 0) for row in rows)
    return round(total / len(rows), 2)


def _to_out(record: AttendanceRecord, member_name: str) -> AttendanceRecordOut:
    display_time = _to_display_checkin_time(record.check_in_time)
    display_str = display_time.strftime("%Y-%m-%d %H:%M:%S")
    return AttendanceRecordOut(
        id=record.id,
        event_id=record.event_id,
        member_id=record.member_id,
        member_name=member_name,
        check_in_time=display_str,
        method=record.method.value,
    )


def _build_history_query(
    event_id: UUID | None,
    member_id: UUID | None,
    event_date: date | None,
) -> Select:
    stmt: Select = (
        select(AttendanceRecord, Member.name)
        .join(Member, AttendanceRecord.member_id == Member.id)
        .join(AttendanceEvent, AttendanceRecord.event_id == AttendanceEvent.id)
    )

    if event_id is not None:
        stmt = stmt.where(AttendanceRecord.event_id == event_id)
    if member_id is not None:
        stmt = stmt.where(AttendanceRecord.member_id == member_id)
    if event_date is not None:
        stmt = stmt.where(AttendanceEvent.event_date == event_date)
    return stmt


@router.get("/current-service", response_model=CurrentServiceInfoResponse)
def current_service_info(db: Session = Depends(get_db)) -> CurrentServiceInfoResponse:
    current = now_local_naive()
    target_date = current.date()
    if not is_sunday(target_date):
        return CurrentServiceInfoResponse(
            date=target_date,
            is_sunday=False,
            event_id=None,
            service_language=None,
            service_title=None,
            event_name=None,
            display_text="非主日",
        )

    language = infer_sunday_service_language(current)
    title = sunday_service_title(language)
    event = get_or_create_sunday_service_event(
        db,
        checkin_time_local=current,
        target_date=target_date,
        language=language,
    )
    event_name = sunday_service_event_name(target_date, language)
    return CurrentServiceInfoResponse(
        date=target_date,
        is_sunday=True,
        event_id=event.id,
        service_language=language,
        service_title=title,
        event_name=event_name,
        display_text=f"{target_date.isoformat()}-{title}",
    )


@router.post("/manual-checkin", response_model=ManualCheckInResponse)
async def manual_checkin(
    payload: ManualCheckInRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ManualCheckInResponse:
    member = db.get(Member, payload.member_id)
    if member is None or not member.status:
        raise HTTPException(status_code=404, detail="member not found or inactive")

    if payload.event_id is not None:
        event = db.get(AttendanceEvent, payload.event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")
        if not is_sunday(event.event_date):
            raise HTTPException(
                status_code=409,
                detail="manual check-in is only allowed on Sunday events",
            )
    else:
        now = now_local_naive()
        target_date = payload.event_date or today_local()
        if not is_sunday(target_date):
            raise HTTPException(
                status_code=409,
                detail="manual check-in is only allowed on Sunday",
            )
        event = get_or_create_sunday_service_event(
            db,
            checkin_time_local=now,
            target_date=target_date,
            language=payload.service_language,
        )

    existing = db.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.event_id == event.id,
            AttendanceRecord.member_id == member.id,
        )
    )
    if existing is not None:
        out = _to_out(existing, member.name)
        return ManualCheckInResponse(status="duplicate", record=out)

    now = now_local_naive()
    record = AttendanceRecord(
        event_id=event.id,
        member_id=member.id,
        check_in_time=now,
        method=CheckInMethod.MANUAL_RECEPTION,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    out = _to_out(record, member.name)

    ws_manager = getattr(request.app.state, "ws_manager", None)
    payload = {
        "event_type": "check_in",
        "method": "manual_reception",
        "timestamp": now_local().isoformat(timespec="seconds"),
        "subject_id": str(member.id),
        "member_id": str(member.id),
        "member_name": member.name,
        "member_name_chn": member.name_chn,
        "event_id": str(event.id),
        "attendance_record_id": str(record.id),
        "persist_status": "ok",
    }
    record_feed_event(db, payload, source="manual_reception")
    db.commit()
    if ws_manager is not None:
        await ws_manager.broadcast_channel_a(payload)

    return ManualCheckInResponse(status="ok", record=out)


@router.get("/history", response_model=AttendanceHistoryResponse)
def attendance_history(
    event_id: UUID | None = Query(default=None),
    member_id: UUID | None = Query(default=None),
    event_date: date | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> AttendanceHistoryResponse:
    stmt = _build_history_query(event_id, member_id, event_date)
    stmt = stmt.order_by(AttendanceRecord.check_in_time.desc()).limit(limit)
    rows = db.execute(stmt).all()

    items: list[AttendanceRecordOut] = []
    for row in rows:
        record, member_name = row
        items.append(_to_out(record, member_name))

    return AttendanceHistoryResponse(total=len(items), items=items)


@router.get("/history/export.csv")
def attendance_history_export_csv(
    event_id: UUID | None = Query(default=None),
    member_id: UUID | None = Query(default=None),
    event_date: date | None = Query(default=None),
    limit: int = Query(default=5000, ge=1, le=20000),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    stmt = _build_history_query(event_id, member_id, event_date)
    stmt = stmt.order_by(AttendanceRecord.check_in_time.desc()).limit(limit)
    rows = db.execute(stmt).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["attendance_record_id", "event_id", "member_id", "member_name", "check_in_time", "method"])
    for row in rows:
        record, member_name = row
        writer.writerow(
            [
                str(record.id),
                str(record.event_id),
                str(record.member_id),
                member_name,
                _to_display_checkin_time(record.check_in_time).strftime("%Y-%m-%d %H:%M:%S"),
                record.method.value,
            ]
        )

    csv_text = buf.getvalue()
    buf.close()

    filename = "attendance_history.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(iter([csv_text]), media_type="text/csv; charset=utf-8", headers=headers)


@router.delete("/history/{record_id}", response_model=AttendanceRecordDeleteResponse)
def delete_attendance_history_record(
    record_id: UUID,
    db: Session = Depends(get_db),
) -> AttendanceRecordDeleteResponse:
    record = db.get(AttendanceRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    member = db.get(Member, record.member_id)
    member_name = member.name if member is not None else "(unknown member)"
    deleted_record = _to_out(record, member_name)

    db.delete(record)
    db.commit()

    payload = {
        "event_type": "check_in_ignored",
        "method": record.method.value,
        "timestamp": now_local().isoformat(timespec="seconds"),
        "subject_id": str(record.member_id),
        "member_id": str(record.member_id),
        "member_name": member_name,
        "member_name_chn": member.name_chn if member is not None else None,
        "event_id": str(record.event_id),
        "attendance_record_id": str(record.id),
        "persist_status": "cancelled",
        "detail": "manual_reception_cancelled",
    }
    record_feed_event(db, payload, source="manual_reception")
    db.commit()

    return AttendanceRecordDeleteResponse(status="deleted", record=deleted_record)


@router.get("/dashboard", response_model=AttendanceDashboardResponse)
def attendance_dashboard(
    days: int = Query(default=14, ge=1, le=180),
    top_events_limit: int = Query(default=8, ge=1, le=50),
    db: Session = Depends(get_db),
) -> AttendanceDashboardResponse:
    today = today_local()
    latest_sunday = _latest_sunday(today)
    sunday_dates = [latest_sunday - timedelta(days=7 * i) for i in range(max(1, days))]
    sunday_dates.reverse()

    active_members = int(
        db.scalar(select(func.count()).select_from(Member).where(Member.status.is_(True))) or 0
    )
    active_events = int(
        db.scalar(select(func.count()).select_from(AttendanceEvent).where(AttendanceEvent.is_archived.is_(False))) or 0
    )
    total_checkins = int(db.scalar(select(func.count()).select_from(AttendanceRecord)) or 0)
    auto_checkins = int(
        db.scalar(
            select(func.count())
            .select_from(AttendanceRecord)
            .where(AttendanceRecord.method == CheckInMethod.AUTO_FACE)
        )
        or 0
    )
    manual_checkins = int(
        db.scalar(
            select(func.count())
            .select_from(AttendanceRecord)
            .where(AttendanceRecord.method == CheckInMethod.MANUAL_RECEPTION)
        )
        or 0
    )
    pending_unknown_cases = int(
        db.scalar(
            select(func.count())
            .select_from(UnknownFaceCase)
            .where(UnknownFaceCase.status == UnknownCaseStatus.PENDING)
        )
        or 0
    )

    daily_rows = db.execute(
        select(AttendanceEvent.event_date, func.count(AttendanceRecord.id))
        .join(AttendanceRecord, AttendanceRecord.event_id == AttendanceEvent.id)
        .where(
            AttendanceEvent.event_date.in_(sunday_dates),
            func.strftime("%w", AttendanceEvent.event_date) == "0",
        )
        .group_by(AttendanceEvent.event_date)
        .order_by(AttendanceEvent.event_date.asc())
    ).all()
    daily_map = {row[0]: int(row[1]) for row in daily_rows}
    daily: list[AttendanceDashboardDailyPoint] = []
    for sunday in sunday_dates:
        daily.append(AttendanceDashboardDailyPoint(event_date=sunday, checkins=daily_map.get(sunday, 0)))

    avg_this_year_zh = _calc_this_year_avg_attendance_by_language(db, today=today, language="zh")
    avg_this_year_it = _calc_this_year_avg_attendance_by_language(db, today=today, language="it")

    top_rows = db.execute(
        select(
            AttendanceEvent.id,
            AttendanceEvent.event_name,
            AttendanceEvent.event_date,
            func.count(AttendanceRecord.id),
        )
        .join(AttendanceRecord, AttendanceRecord.event_id == AttendanceEvent.id)
        .group_by(AttendanceEvent.id, AttendanceEvent.event_name, AttendanceEvent.event_date)
        .order_by(func.count(AttendanceRecord.id).desc(), AttendanceEvent.event_date.desc())
        .limit(top_events_limit)
    ).all()
    top_events = [
        AttendanceDashboardEventPoint(
            event_id=row[0],
            event_name=row[1],
            event_date=row[2],
            checkins=int(row[3]),
        )
        for row in top_rows
    ]

    return AttendanceDashboardResponse(
        summary=AttendanceDashboardSummary(
            active_members=active_members,
            active_events=active_events,
            total_checkins=total_checkins,
            auto_checkins=auto_checkins,
            manual_checkins=manual_checkins,
            pending_unknown_cases=pending_unknown_cases,
            avg_this_year_zh=avg_this_year_zh,
            avg_this_year_it=avg_this_year_it,
        ),
        daily=daily,
        top_events=top_events,
    )
