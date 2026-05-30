from __future__ import annotations

import re
from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.time_utils import now_local_naive
from app.models.models import Member
from app.models.models import AttendanceRecord
from app.schemas.member import (
    MemberCreateRequest,
    MemberListResponse,
    MemberOut,
    MemberSearchResponse,
    MemberUpdateRequest,
)

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # pragma: no cover - optional dependency fallback
    Style = None
    lazy_pinyin = None

router = APIRouter(prefix="/members", tags=["members"])


def _calculate_age_from_birthday(birthday: date | None) -> int | None:
    if birthday is None:
        return None
    today = date.today()
    years = today.year - birthday.year
    if (today.month, today.day) < (birthday.month, birthday.day):
        years -= 1
    if years < 0 or years > 130:
        return None
    return years


def _to_member_out(member: Member) -> MemberOut:
    computed_age = _calculate_age_from_birthday(member.birthday)
    return MemberOut(
        id=member.id,
        name=member.name,
        name_chn=member.name_chn,
        age=computed_age,
        has_photo=member.has_photo,
        gender=member.gender,
        group=member.group,
        birthday=member.birthday,
        note=member.note,
        status=member.status,
        attendance_status=None,
        attendance_record_id=None,
        attendance_event_id=None,
    )


def _normalize_search_text(value: str | None) -> str:
    return (value or "").strip().lower()


def _gender_filter_values(gender: str | None) -> list[str]:
    gender_value = (gender or "").strip().lower()
    if not gender_value:
        return []

    if gender_value in {"male", "m", "man", "男"}:
        return ["male", "男"]
    if gender_value in {"female", "f", "woman", "女"}:
        return ["female", "女"]
    return [gender_value]


def _initials_from_text(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    tokens = [part for part in re.split(r"[^A-Za-z0-9\u00C0-\u024F]+", raw) if part]
    if not tokens:
        return ""
    return "".join((token[0] or "").upper() for token in tokens)


def _pinyin_initials_from_text(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw or lazy_pinyin is None or Style is None:
        return ""

    try:
        initials = lazy_pinyin(raw, style=Style.FIRST_LETTER, errors=lambda item: list(item))
    except Exception:
        return ""
    return "".join(part.upper() for part in initials if part)


def _pinyin_full_from_text(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw or lazy_pinyin is None or Style is None:
        return ""

    try:
        parts = lazy_pinyin(raw, style=Style.NORMAL, errors=lambda item: list(item))
    except Exception:
        return ""
    return "".join(part.lower() for part in parts if part)


def _matches_member_keyword(member: Member, keyword: str) -> bool:
    normalized = _normalize_search_text(keyword)
    if not normalized:
        return True

    fields = [member.name, member.name_chn, member.group, member.note]
    for field in fields:
        if field and normalized in _normalize_search_text(field):
            return True

    acronym = re.sub(r"\s+", "", normalized)
    if acronym and acronym.isalnum():
        candidate_names = [member.name, member.name_chn]
        for candidate in candidate_names:
            initials = _initials_from_text(candidate)
            pinyin_initials = _pinyin_initials_from_text(candidate)
            pinyin_full = _pinyin_full_from_text(candidate)
            compact = re.sub(r"\s+", "", _normalize_search_text(candidate)) if candidate else ""
            if initials and acronym == initials.lower():
                return True
            if pinyin_initials and acronym == pinyin_initials.lower():
                return True
            if pinyin_full and acronym in pinyin_full:
                return True
            if compact and acronym in compact:
                return True

    return False


@router.post("", response_model=MemberOut)
def create_member(payload: MemberCreateRequest, db: Session = Depends(get_db)) -> MemberOut:
    row = Member(
        name=payload.name.strip(),
        name_chn=(payload.name_chn.strip() if payload.name_chn else None),
        gender=(payload.gender.strip().lower() if payload.gender else None),
        group=(payload.group.strip() if payload.group else None),
        birthday=payload.birthday,
        note=(payload.note.strip() if payload.note else None),
        status=payload.status,
    )
    if not row.name:
        raise HTTPException(status_code=400, detail="name is required")

    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_member_out(row)


@router.get("", response_model=MemberListResponse)
def list_members(
    active_only: bool | None = Query(default=None),
    status_filter: str = Query(default="active", pattern="^(all|active|inactive)$"),
    q: str = Query(default="", max_length=120),
    has_photo_filter: str = Query(default="all", pattern="^(all|with_photo|without_photo)$"),
    gender: str | None = Query(default=None, max_length=16),
    group: str | None = Query(default=None, max_length=120),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> MemberListResponse:
    stmt: Select = select(Member)

    normalized_filter = status_filter
    if active_only is not None:
        normalized_filter = "active" if active_only else "all"

    if normalized_filter == "active":
        stmt = stmt.where(Member.status.is_(True))
    elif normalized_filter == "inactive":
        stmt = stmt.where(Member.status.is_(False))

    keyword = q.strip()
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                Member.name.ilike(like),
                Member.name_chn.ilike(like),
                Member.group.ilike(like),
                Member.note.ilike(like),
            )
        )

    if has_photo_filter == "with_photo":
        stmt = stmt.where(Member.has_photo.is_(True))
    elif has_photo_filter == "without_photo":
        stmt = stmt.where(Member.has_photo.is_(False))

    gender_values = _gender_filter_values(gender)
    if gender_values:
        stmt = stmt.where(Member.gender.in_(gender_values))

    group_name = (group or "").strip()
    if group_name:
        stmt = stmt.where(Member.group == group_name)

    rows = db.execute(stmt.order_by(Member.name.asc()).offset(offset).limit(limit)).scalars().all()
    items = [_to_member_out(member) for member in rows]
    return MemberListResponse(total=len(items), items=items)


@router.get("/search", response_model=MemberSearchResponse)
def search_members(
    q: str = Query(default="", max_length=120),
    active_only: bool = Query(default=True),
    event_id: UUID | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=200),
    db: Session = Depends(get_db),
) -> MemberSearchResponse:
    stmt: Select = select(Member)

    keyword = q.strip()
    acronym = re.sub(r"\s+", "", keyword).lower()
    use_sql_like = not (keyword and acronym.isalnum())
    if keyword and use_sql_like:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                Member.name.ilike(like),
                Member.name_chn.ilike(like),
                Member.group.ilike(like),
                Member.note.ilike(like),
            )
        )

    if active_only:
        stmt = stmt.where(Member.status.is_(True))

    fetch_limit = min(limit * 20, 1000) if keyword and not use_sql_like else limit * 4
    rows = db.execute(stmt.order_by(Member.name.asc()).limit(fetch_limit)).scalars().all()
    rows = db.execute(stmt.order_by(Member.name.asc()).limit(fetch_limit)).scalars().all()
    members = [member for member in rows if _matches_member_keyword(member, keyword)][:limit]

    attendance_map: dict[UUID, AttendanceRecord] = {}
    if event_id is not None and members:
        member_ids = [member.id for member in members]
        records = db.execute(
            select(AttendanceRecord).where(
                AttendanceRecord.event_id == event_id,
                AttendanceRecord.member_id.in_(member_ids),
            )
        ).scalars().all()
        attendance_map = {record.member_id: record for record in records}

    items = []
    for member in members:
        out = _to_member_out(member)
        if event_id is not None:
            record = attendance_map.get(member.id)
            if record is not None:
                out.attendance_status = "checked_in"
                out.attendance_record_id = record.id
                out.attendance_event_id = record.event_id
            else:
                out.attendance_status = "not_checked_in"
                out.attendance_event_id = event_id
        items.append(out)
    return MemberSearchResponse(total=len(items), items=items)


@router.get("/photo-picker", response_model=MemberSearchResponse)
def photo_picker_members(
    event_id: UUID | None = Query(default=None),
    gender: str | None = Query(default=None, max_length=16),
    include_no_photo: bool = Query(default=False),
    active_only: bool = Query(default=True),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> MemberSearchResponse:
    since = now_local_naive() - timedelta(days=90)
    recent_count = func.count(AttendanceRecord.id).label("recent_count")
    stmt: Select = (
        select(Member, recent_count)
        .outerjoin(
            AttendanceRecord,
            and_(
                AttendanceRecord.member_id == Member.id,
                AttendanceRecord.check_in_time >= since,
            ),
        )
        .group_by(Member.id)
    )

    if not include_no_photo:
        stmt = stmt.where(Member.has_photo.is_(True))

    if active_only:
        stmt = stmt.where(Member.status.is_(True))

    gender_values = _gender_filter_values(gender)
    if gender_values:
        stmt = stmt.where(Member.gender.in_(gender_values))

    if event_id is not None:
        checked_in = select(AttendanceRecord.member_id).where(AttendanceRecord.event_id == event_id)
        stmt = stmt.where(~Member.id.in_(checked_in))

    stmt = stmt.order_by(recent_count.desc(), Member.name.asc()).offset(offset).limit(limit)
    rows = db.execute(stmt).all()

    items: list[MemberOut] = []
    for row in rows:
        member = row[0]
        out = _to_member_out(member)
        if event_id is not None:
            out.attendance_status = "not_checked_in"
            out.attendance_event_id = event_id
        items.append(out)

    return MemberSearchResponse(total=len(items), items=items)


@router.get("/transliterate")
def transliterate_text(q: str = Query(default="", max_length=120)) -> dict[str, str]:
    text = q.strip()
    if not text:
        return {"original": "", "pinyin": ""}

    pinyin = _pinyin_full_from_text(text)
    return {
        "original": text,
        "pinyin": pinyin or text,
    }


@router.get("/{member_id}", response_model=MemberOut)
def get_member(member_id: UUID, db: Session = Depends(get_db)) -> MemberOut:
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="member not found")
    return _to_member_out(member)


@router.put("/{member_id}", response_model=MemberOut)
def update_member(member_id: UUID, payload: MemberUpdateRequest, db: Session = Depends(get_db)) -> MemberOut:
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="member not found")

    provided_fields = set(getattr(payload, "model_fields_set", set()))

    if "name" in provided_fields:
        member.name = (payload.name or "").strip()
        if not member.name:
            raise HTTPException(status_code=400, detail="name cannot be empty")
    if "name_chn" in provided_fields:
        member.name_chn = payload.name_chn.strip() if payload.name_chn else None
    if "gender" in provided_fields:
        member.gender = payload.gender.strip().lower() if payload.gender else None
    if "group" in provided_fields:
        member.group = payload.group.strip() if payload.group else None
    if "birthday" in provided_fields:
        member.birthday = payload.birthday
    if "note" in provided_fields:
        member.note = payload.note.strip() if payload.note else None
    if "status" in provided_fields:
        if payload.status is None:
            raise HTTPException(status_code=400, detail="status cannot be null")
        member.status = payload.status

    db.commit()
    db.refresh(member)
    return _to_member_out(member)


@router.delete("/{member_id}")
def deactivate_member(member_id: UUID, db: Session = Depends(get_db)) -> dict[str, str]:
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="member not found")

    member.status = False
    db.commit()
    return {
        "status": "deactivated",
        "member_id": str(member_id),
        "message": "member logically deactivated; history is preserved",
    }


@router.post("/{member_id}/restore")
def restore_member(member_id: UUID, db: Session = Depends(get_db)) -> dict[str, str]:
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="member not found")

    member.status = True
    db.commit()
    return {
        "status": "restored",
        "member_id": str(member_id),
        "message": "member restored to active status",
    }
