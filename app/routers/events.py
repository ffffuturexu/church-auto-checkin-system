from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, func, not_, or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.models import AttendanceEvent
from app.schemas.event import (
    AttendanceEventArchiveRequest,
    AttendanceEventCreateRequest,
    AttendanceEventListResponse,
    AttendanceEventOut,
    AttendanceEventUpdateRequest,
)

router = APIRouter(prefix="/events", tags=["events"])


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


def _to_out(row: AttendanceEvent) -> AttendanceEventOut:
    return AttendanceEventOut(
        id=row.id,
        event_name=row.event_name,
        event_date=row.event_date,
        is_archived=bool(row.is_archived),
    )


@router.get("", response_model=AttendanceEventListResponse)
def list_events(
    include_archived: bool = Query(default=False),
    year: int | None = Query(default=None, ge=2000, le=2100),
    language: str = Query(default="all"),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> AttendanceEventListResponse:
    stmt: Select = select(AttendanceEvent)
    if not include_archived:
        stmt = stmt.where(AttendanceEvent.is_archived.is_(False))

    if year is not None:
        start = date(year, 1, 1)
        end = date(year, 12, 31)
        stmt = stmt.where(
            AttendanceEvent.event_date >= start,
            AttendanceEvent.event_date <= end,
        )

    normalized_language = (language or "all").strip().lower()
    zh_filter, it_filter = _event_language_filters()
    if normalized_language == "zh":
        stmt = stmt.where(zh_filter)
    elif normalized_language == "it":
        stmt = stmt.where(it_filter)
    elif normalized_language == "other":
        stmt = stmt.where(not_(or_(zh_filter, it_filter)))
    elif normalized_language != "all":
        raise HTTPException(status_code=400, detail="invalid language, expected all|zh|it|other")

    rows = db.execute(stmt.order_by(AttendanceEvent.event_date.desc()).limit(limit)).scalars().all()
    items = [_to_out(row) for row in rows]
    return AttendanceEventListResponse(total=len(items), items=items)


@router.post("", response_model=AttendanceEventOut)
def create_event(payload: AttendanceEventCreateRequest, db: Session = Depends(get_db)) -> AttendanceEventOut:
    row = AttendanceEvent(
        event_name=payload.event_name.strip(),
        event_date=payload.event_date,
        is_archived=False,
    )
    if not row.event_name:
        raise HTTPException(status_code=400, detail="event_name is required")

    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.get("/{event_id}", response_model=AttendanceEventOut)
def get_event(event_id: UUID, db: Session = Depends(get_db)) -> AttendanceEventOut:
    row = db.get(AttendanceEvent, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")
    return _to_out(row)


@router.put("/{event_id}", response_model=AttendanceEventOut)
def update_event(
    event_id: UUID,
    payload: AttendanceEventUpdateRequest,
    db: Session = Depends(get_db),
) -> AttendanceEventOut:
    row = db.get(AttendanceEvent, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")

    if payload.event_name is not None:
        row.event_name = payload.event_name.strip()
        if not row.event_name:
            raise HTTPException(status_code=400, detail="event_name cannot be empty")
    if payload.event_date is not None:
        row.event_date = payload.event_date

    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.post("/{event_id}/archive", response_model=AttendanceEventOut)
def archive_event(
    event_id: UUID,
    payload: AttendanceEventArchiveRequest,
    db: Session = Depends(get_db),
) -> AttendanceEventOut:
    row = db.get(AttendanceEvent, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")

    row.is_archived = payload.is_archived
    db.commit()
    db.refresh(row)
    return _to_out(row)
