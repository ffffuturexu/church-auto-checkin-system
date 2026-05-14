from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.service_event import get_or_create_sunday_service_event, is_sunday
from app.core.time_utils import now_local, now_local_naive
from app.models.models import (
    AttendanceEvent,
    AttendanceRecord,
    CheckInMethod,
    Member,
    RecognitionLog,
    UnknownCaseStatus,
    UnknownFaceCase,
)
from app.schemas.reception_queue import (
    UnknownFaceCaseActionResponse,
    UnknownFaceClearResponse,
    UnknownFaceCaseListResponse,
    UnknownFaceCaseOut,
    UnknownFaceIgnoreRequest,
    UnknownFaceResolveRequest,
)
from app.services.reception_feed_service import record_feed_event

router = APIRouter(prefix="/reception/queue", tags=["reception-queue"])


_UNKNOWN_CANDIDATE_PATTERN = re.compile(
    r"(best_subject_id|best_subject_name|best_subject_name_chn|best_subject_name_zh|"
    r"second_subject_id|second_subject_name)=([^;|]+)"
)


def _normalize_primary_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _parse_unknown_candidate_from_note(note: str | None) -> dict[str, str | None]:
    values: dict[str, str | None] = {
        "best_subject_id": None,
        "best_subject_name": None,
        "best_subject_name_chn": None,
        "second_subject_id": None,
        "second_subject_name": None,
    }
    if not note:
        return values

    for key, raw_value in _UNKNOWN_CANDIDATE_PATTERN.findall(str(note)):
        clean = raw_value.strip()
        if clean:
            if key == "best_subject_name_zh":
                values["best_subject_name_chn"] = clean
            else:
                values[key] = clean
    return values


def _resolve_member_name_chn_by_subject(db: Session, subject_id: str | None) -> str | None:
    if not subject_id:
        return None
    try:
        subject_uuid = UUID(str(subject_id))
    except ValueError:
        return None
    stmt = select(Member.name_chn).where(Member.id == subject_uuid)
    name_chn = db.scalar(stmt)
    return str(name_chn).strip() if name_chn else None


def _resolve_member_name_chn_by_name(db: Session, name: str | None) -> str | None:
    if not name:
        return None
    stmt = select(Member.name_chn).where(func.lower(Member.name) == name.strip().lower())
    name_chn = db.scalar(stmt)
    return str(name_chn).strip() if name_chn else None


def _resolve_unknown_candidate_from_log(db: Session, row: UnknownFaceCase) -> dict[str, str | None]:
    values: dict[str, str | None] = {
        "best_subject_id": None,
        "best_subject_name": None,
        "best_subject_name_chn": None,
        "second_subject_id": None,
        "second_subject_name": None,
    }

    if not row.reason or not row.timestamp:
        return values

    if row.reason.lower() not in {"failed_threshold", "failed_margin"}:
        return values

    start = row.timestamp - timedelta(seconds=2)
    end = row.timestamp + timedelta(seconds=2)
    logs = db.execute(
        select(RecognitionLog)
        .where(
            RecognitionLog.timestamp >= start,
            RecognitionLog.timestamp <= end,
            func.lower(RecognitionLog.status) == row.reason.lower(),
        )
        .order_by(RecognitionLog.timestamp.desc())
        .limit(20)
    ).scalars().all()

    if not logs:
        return values

    best_log = min(logs, key=lambda item: abs((item.timestamp - row.timestamp).total_seconds()))
    best_subject_id = str(best_log.best_subject_id).strip() if best_log.best_subject_id else None
    if best_subject_id in {"", "unknown"}:
        best_subject_id = None

    second_subject_id = str(best_log.second_subject_id).strip() if best_log.second_subject_id else None
    if second_subject_id == "":
        second_subject_id = None

    values["best_subject_id"] = best_subject_id
    values["best_subject_name"] = (
        str(best_log.best_subject_name).strip() if best_log.best_subject_name else best_subject_id
    )
    values["best_subject_name_chn"] = _resolve_member_name_chn_by_subject(db, best_subject_id)
    values["second_subject_id"] = second_subject_id
    values["second_subject_name"] = (
        str(best_log.second_subject_name).strip() if best_log.second_subject_name else second_subject_id
    )
    return values


def _resolve_unknown_candidate(db: Session, row: UnknownFaceCase) -> dict[str, str | None]:
    candidate = _parse_unknown_candidate_from_note(row.note)
    if candidate["best_subject_id"] or candidate["best_subject_name"] or candidate["best_subject_name_chn"]:
        if not candidate["best_subject_name_chn"]:
            candidate["best_subject_name_chn"] = _resolve_member_name_chn_by_subject(
                db, candidate["best_subject_id"]
            ) or _resolve_member_name_chn_by_name(db, candidate["best_subject_name"])
        return candidate

    fallback = _resolve_unknown_candidate_from_log(db, row)
    for key, value in fallback.items():
        if not candidate[key] and value:
            candidate[key] = value
    if not candidate["best_subject_name_chn"]:
        candidate["best_subject_name_chn"] = _resolve_member_name_chn_by_subject(
            db, candidate["best_subject_id"]
        ) or _resolve_member_name_chn_by_name(db, candidate["best_subject_name"])
    return candidate


def _to_out(db: Session, row: UnknownFaceCase) -> UnknownFaceCaseOut:
    candidate = _resolve_unknown_candidate(db, row)
    return UnknownFaceCaseOut(
        id=row.id,
        timestamp=row.timestamp,
        reason=row.reason,
        image_base64=row.image_base64,
        best_subject_id=candidate["best_subject_id"],
        best_subject_name=candidate["best_subject_name"],
        best_subject_name_chn=candidate["best_subject_name_chn"],
        second_subject_id=candidate["second_subject_id"],
        second_subject_name=candidate["second_subject_name"],
        status=row.status.value,
        member_id=row.member_id,
        event_id=row.event_id,
        attendance_record_id=row.attendance_record_id,
        note=row.note,
        handled_at=row.handled_at,
    )


def _mark_case_ignored_non_sunday(
    db: Session,
    row: UnknownFaceCase,
    member: Member,
    payload: UnknownFaceResolveRequest,
) -> UnknownFaceCaseActionResponse:
    resolved_candidate = _resolve_unknown_candidate(db, row)
    sibling_ignored_count = _auto_ignore_sibling_pending_cases(
        db,
        resolved_case=row,
        resolved_member=member,
        resolved_candidate=resolved_candidate,
    )

    row.status = UnknownCaseStatus.IGNORED
    note_parts: list[str] = []
    if payload.note and payload.note.strip():
        note_parts.append(payload.note.strip())
    if row.note and row.note.strip():
        note_parts.append(row.note.strip())
    note_parts.append("auto_ignored_non_sunday_manual_checkin")
    row.note = " | ".join(note_parts)[:255]
    row.handled_at = now_local_naive()
    db.commit()
    db.refresh(row)

    message = "manual check-in ignored: non_sunday"
    if sibling_ignored_count > 0:
        message = f"{message}; auto_ignored_siblings={sibling_ignored_count}"

    return UnknownFaceCaseActionResponse(
        status="ignored",
        case=_to_out(db, row),
        message=message,
    )


def _append_case_note(base_note: str | None, suffix: str) -> str:
    parts: list[str] = []
    if base_note and base_note.strip():
        parts.append(base_note.strip())
    parts.append(suffix)
    return " | ".join(parts)[:255]


def _is_same_primary_candidate(
    candidate: dict[str, str | None],
    id_anchors: set[str],
    name_anchors: set[str],
) -> bool:
    best_id = str(candidate.get("best_subject_id") or "").strip()
    best_name = _normalize_primary_text(candidate.get("best_subject_name"))

    if best_id and best_id in id_anchors:
        return True
    if best_name and best_name in name_anchors:
        return True
    return False


def _auto_ignore_sibling_pending_cases(
    db: Session,
    resolved_case: UnknownFaceCase,
    resolved_member: Member,
    resolved_candidate: dict[str, str | None],
) -> int:
    day_start = datetime.combine(resolved_case.timestamp.date(), datetime.min.time())
    day_end = day_start + timedelta(days=1)

    id_anchors: set[str] = {str(resolved_member.id)}
    best_id = str(resolved_candidate.get("best_subject_id") or "").strip()
    if best_id:
        id_anchors.add(best_id)

    name_anchors: set[str] = {_normalize_primary_text(resolved_member.name)}
    best_name = _normalize_primary_text(resolved_candidate.get("best_subject_name"))
    if best_name:
        name_anchors.add(best_name)

    rows = db.execute(
        select(UnknownFaceCase)
        .where(
            UnknownFaceCase.status == UnknownCaseStatus.PENDING,
            UnknownFaceCase.id != resolved_case.id,
            UnknownFaceCase.timestamp >= day_start,
            UnknownFaceCase.timestamp < day_end,
        )
        .order_by(UnknownFaceCase.timestamp.asc())
    ).scalars().all()

    ignored_count = 0
    now = now_local_naive()
    for row in rows:
        candidate = _resolve_unknown_candidate(db, row)
        if not _is_same_primary_candidate(candidate, id_anchors=id_anchors, name_anchors=name_anchors):
            continue

        row.status = UnknownCaseStatus.IGNORED
        row.note = _append_case_note(row.note, "auto_ignored_after_manual_resolve_same_day_primary")
        row.handled_at = now
        ignored_count += 1

    return ignored_count


@router.get("/unknown", response_model=UnknownFaceCaseListResponse)
def list_unknown_queue(
    status: str = Query(default="pending"),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> UnknownFaceCaseListResponse:
    stmt: Select = select(UnknownFaceCase)
    normalized = status.strip().lower()
    if normalized:
        try:
            enum_value = UnknownCaseStatus(normalized)
            stmt = stmt.where(UnknownFaceCase.status == enum_value)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid status")

    rows = db.execute(stmt.order_by(UnknownFaceCase.timestamp.desc()).limit(limit)).scalars().all()
    items = [_to_out(db, row) for row in rows]
    return UnknownFaceCaseListResponse(total=len(items), items=items)


@router.post("/unknown/{case_id}/ignore", response_model=UnknownFaceCaseActionResponse)
def ignore_unknown_case(
    case_id: UUID,
    payload: UnknownFaceIgnoreRequest | None = None,
    db: Session = Depends(get_db),
) -> UnknownFaceCaseActionResponse:
    row = db.get(UnknownFaceCase, case_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown case not found")

    row.status = UnknownCaseStatus.IGNORED
    row.note = (payload.note.strip() if payload and payload.note else None)
    row.handled_at = now_local_naive()
    db.commit()
    db.refresh(row)

    return UnknownFaceCaseActionResponse(status="ignored", case=_to_out(db, row), message=None)


@router.post("/unknown/clear", response_model=UnknownFaceClearResponse)
def clear_unknown_queue(
    payload: UnknownFaceIgnoreRequest | None = None,
    db: Session = Depends(get_db),
) -> UnknownFaceClearResponse:
    rows = db.execute(
        select(UnknownFaceCase).where(UnknownFaceCase.status == UnknownCaseStatus.PENDING)
    ).scalars().all()

    note = payload.note.strip() if payload and payload.note else None
    now = now_local_naive()
    for row in rows:
        row.status = UnknownCaseStatus.IGNORED
        row.handled_at = now
        if note:
            row.note = note

    db.commit()
    return UnknownFaceClearResponse(status="ok", cleared=len(rows))


@router.post("/unknown/{case_id}/resolve", response_model=UnknownFaceCaseActionResponse)
async def resolve_unknown_case(
    case_id: UUID,
    payload: UnknownFaceResolveRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> UnknownFaceCaseActionResponse:
    row = db.get(UnknownFaceCase, case_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown case not found")

    member = db.get(Member, payload.member_id)
    if member is None or not member.status:
        raise HTTPException(status_code=404, detail="member not found or inactive")

    if payload.event_id is not None:
        existing_event = db.get(AttendanceEvent, payload.event_id)
        if existing_event is None:
            raise HTTPException(status_code=404, detail="event not found")
        if not is_sunday(existing_event.event_date):
            return _mark_case_ignored_non_sunday(db, row, member, payload)

    target_date = payload.event_date or row.timestamp.date()
    if payload.event_id is None and not is_sunday(target_date):
        return _mark_case_ignored_non_sunday(db, row, member, payload)

    event = _pick_event_for_resolution(
        db,
        event_id=payload.event_id,
        event_date=payload.event_date,
        default_timestamp=row.timestamp,
        service_language=payload.service_language,
    )

    existing = db.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.event_id == event.id,
            AttendanceRecord.member_id == member.id,
        )
    )
    message = None
    if existing is None:
        record = AttendanceRecord(
            event_id=event.id,
            member_id=member.id,
            check_in_time=now_local_naive(),
            method=CheckInMethod.MANUAL_RECEPTION,
        )
        db.add(record)
        db.flush()
    else:
        record = existing
        message = "member already checked in for selected event"

    resolved_candidate = _resolve_unknown_candidate(db, row)
    sibling_ignored_count = _auto_ignore_sibling_pending_cases(
        db,
        resolved_case=row,
        resolved_member=member,
        resolved_candidate=resolved_candidate,
    )

    row.status = UnknownCaseStatus.RESOLVED
    row.member_id = member.id
    row.event_id = event.id
    row.attendance_record_id = record.id
    user_note = payload.note.strip() if payload.note else None
    row.note = _append_case_note(user_note or row.note, "manual_resolve")
    row.handled_at = now_local_naive()

    db.commit()
    db.refresh(row)

    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
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
        await ws_manager.broadcast_channel_a(payload)

    messages: list[str] = []
    if message:
        messages.append(message)
    if sibling_ignored_count > 0:
        messages.append(f"auto_ignored_siblings={sibling_ignored_count}")

    return UnknownFaceCaseActionResponse(
        status="resolved",
        case=_to_out(db, row),
        message="; ".join(messages) if messages else None,
    )


def _pick_event_for_resolution(
    db: Session,
    event_id: UUID | None,
    event_date: date | None,
    default_timestamp: datetime,
    service_language: str | None,
) -> AttendanceEvent:
    if event_id is not None:
        row = db.get(AttendanceEvent, event_id)
        if row is None:
            raise HTTPException(status_code=404, detail="event not found")
        return row

    target_date = event_date or default_timestamp.date()
    checkin_time_local = default_timestamp
    if event_date is not None and event_date != default_timestamp.date():
        checkin_time_local = datetime.combine(event_date, default_timestamp.time())

    return get_or_create_sunday_service_event(
        db,
        checkin_time_local=checkin_time_local,
        target_date=target_date,
        language=service_language,
    )
