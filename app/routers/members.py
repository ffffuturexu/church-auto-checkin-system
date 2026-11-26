from __future__ import annotations

import re
from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.time_utils import now_local_naive, today_local
from app.models.models import AttendanceEvent, AttendanceRecord, Member, RelationshipGroup, RelationshipMembership
from app.schemas.member import (
    ExpandedSearchGroup,
    ExpandedSearchMember,
    ExpandedSearchResponse,
    MemberCreateRequest,
    MemberListResponse,
    MemberOut,
    MemberSearchResponse,
    MemberUpdateRequest,
    RelationshipGroupList,
    RelationshipGroupOut,
    RelationshipGroupUpdate,
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
        birthday_month=member.birthday_month,
        birthday_day=member.birthday_day,
        age_at_record=member.age_at_record,
        age_recorded_on=member.age_recorded_on,
        note=member.note,
        status=member.status,
        attendance_status=None,
        attendance_record_id=None,
        attendance_event_id=None,
    )


def _validate_birthday_fields(birthday: date | None, month: int | None, day: int | None, age: int | None) -> None:
    if birthday is not None and birthday > today_local():
        raise HTTPException(status_code=422, detail="birthday cannot be in the future")
    if (month is None) != (day is None):
        raise HTTPException(status_code=422, detail="birthday_month and birthday_day must be provided together")
    if month is not None and day is not None:
        try:
            date(2000, month, day)
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid birthday month and day")
    if birthday is not None and (month is not None or age is not None):
        raise HTTPException(status_code=422, detail="birthday cannot coexist with partial birthday or recorded age")


def _matching_create(existing: Member, candidate: Member) -> bool:
    fields = ("name", "name_chn", "gender", "group", "birthday", "birthday_month",
              "birthday_day", "age_at_record", "note", "status")
    return all(getattr(existing, field) == getattr(candidate, field) for field in fields)


def _existing_create_result(existing: Member, candidate: Member) -> MemberOut:
    if not _matching_create(existing, candidate):
        raise HTTPException(status_code=409, detail="member id already exists with different content")
    return _to_member_out(existing)


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
    _validate_birthday_fields(payload.birthday, payload.birthday_month, payload.birthday_day, payload.age_at_record)
    row = Member(
        id=payload.id,
        name=payload.name.strip(),
        name_chn=(payload.name_chn.strip() if payload.name_chn else None),
        gender=(payload.gender.strip().lower() if payload.gender else None),
        group=(payload.group.strip() if payload.group else None),
        birthday=payload.birthday,
        birthday_month=payload.birthday_month,
        birthday_day=payload.birthday_day,
        age_at_record=payload.age_at_record,
        age_recorded_on=today_local() if payload.age_at_record is not None else None,
        note=(payload.note.strip() if payload.note else None),
        status=payload.status,
    )
    if not row.name:
        raise HTTPException(status_code=400, detail="name is required")

    if payload.id is not None:
        existing = db.get(Member, payload.id)
        if existing is not None:
            return _existing_create_result(existing, row)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if payload.id is not None:
            existing = db.get(Member, payload.id)
            if existing is not None:
                return _existing_create_result(existing, row)
        raise
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

    rows = db.scalars(
        stmt.order_by(Member.name.asc(), Member.id.asc()).execution_options(yield_per=100)
    )
    members = []
    for member in rows:
        if _matches_member_keyword(member, keyword):
            members.append(member)
            if len(members) == limit:
                break

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


@router.get("/search-with-relations", response_model=ExpandedSearchResponse)
def search_members_with_relations(
    q: str = Query(min_length=1, max_length=120),
    event_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> ExpandedSearchResponse:
    if not q.strip():
        raise HTTPException(status_code=422, detail="搜索内容不能为空")
    if event_id is not None and db.get(AttendanceEvent, event_id) is None:
        raise HTTPException(status_code=404, detail="event not found")

    matches = search_members(q=q, active_only=True, event_id=event_id, limit=31, db=db).items
    truncated = len(matches) > 30
    matches = matches[:30]
    match_ids = {member.id for member in matches}
    memberships = db.scalars(
        select(RelationshipMembership).where(RelationshipMembership.member_id.in_(match_ids))
    ).all() if match_ids else []
    member_groups = {row.member_id: row.group_id for row in memberships}
    group_ids = set(member_groups.values())
    rows = db.execute(
        select(RelationshipMembership.group_id, Member)
        .join(Member, Member.id == RelationshipMembership.member_id)
        .where(RelationshipMembership.group_id.in_(group_ids), Member.status.is_(True))
        .order_by(Member.name, Member.id)
    ).all() if group_ids else []
    records = db.scalars(
        select(AttendanceRecord)
        .join(RelationshipMembership, RelationshipMembership.member_id == AttendanceRecord.member_id)
        .where(
            AttendanceRecord.event_id == event_id, RelationshipMembership.group_id.in_(group_ids)
        )
    ).all() if event_id is not None and group_ids else []
    attendance = {record.member_id: record for record in records}

    groups: list[ExpandedSearchGroup] = []
    grouped: dict[UUID, ExpandedSearchGroup] = {}
    for member in matches:
        group_id = member_groups.get(member.id)
        if group_id is None:
            groups.append(ExpandedSearchGroup(
                group_id=None, members=[ExpandedSearchMember(member=member, direct_match=True)]
            ))
        else:
            if group_id not in grouped:
                grouped[group_id] = ExpandedSearchGroup(group_id=group_id, members=[])
                groups.append(grouped[group_id])
            grouped[group_id].members.append(ExpandedSearchMember(member=member, direct_match=True))
    for group_id, member in rows:
        if member.id in match_ids:
            continue
        out = _to_member_out(member)
        if event_id is not None:
            record = attendance.get(member.id)
            out.attendance_status = "checked_in" if record else "not_checked_in"
            out.attendance_record_id = record.id if record else None
            out.attendance_event_id = event_id
        grouped[group_id].members.append(
            ExpandedSearchMember(member=out, direct_match=_matches_member_keyword(member, q))
        )
    for group in grouped.values():
        group.members.sort(key=lambda entry: not entry.direct_match)
    return ExpandedSearchResponse(groups=groups, truncated=truncated)


@router.get("/photo-picker", response_model=MemberSearchResponse)
def photo_picker_members(
    event_id: UUID | None = Query(default=None),
    gender: str | None = Query(default=None, max_length=16),
    attendance_status: str | None = Query(default=None, max_length=16),
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

    attendance_status_value = (attendance_status or "").strip().lower()
    if attendance_status_value and attendance_status_value not in {"all", "checked_in", "not_checked_in"}:
        raise HTTPException(status_code=400, detail="invalid attendance_status")

    checked_in_member_ids_query = None
    attendance_map: dict[UUID, AttendanceRecord] = {}
    checked_in_order = None
    if event_id is not None:
        records = db.execute(
            select(AttendanceRecord).where(AttendanceRecord.event_id == event_id)
        ).scalars().all()
        attendance_map = {record.member_id: record for record in records}
        checked_in_member_ids_query = select(AttendanceRecord.member_id).where(AttendanceRecord.event_id == event_id)

        if attendance_status_value == "checked_in":
            stmt = stmt.where(Member.id.in_(checked_in_member_ids_query))
            checked_in_order = (
                select(func.max(AttendanceRecord.check_in_time))
                .where(
                    AttendanceRecord.event_id == event_id,
                    AttendanceRecord.member_id == Member.id,
                )
                .correlate(Member)
                .scalar_subquery()
            )
        elif attendance_status_value == "not_checked_in":
            stmt = stmt.where(~Member.id.in_(checked_in_member_ids_query))

    if checked_in_order is not None:
        stmt = stmt.order_by(checked_in_order.desc(), Member.name.asc())
    else:
        stmt = stmt.order_by(recent_count.desc(), Member.name.asc())
    stmt = stmt.offset(offset).limit(limit)
    rows = db.execute(stmt).all()

    items: list[MemberOut] = []
    for row in rows:
        member = row[0]
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


@router.get("/relationship-groups/photo-picker", response_model=RelationshipGroupList)
def relationship_group_photo_picker(
    event_id: UUID,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> RelationshipGroupList:
    if db.get(AttendanceEvent, event_id) is None:
        raise HTTPException(status_code=404, detail="event not found")

    checked = (
        select(AttendanceRecord.id)
        .where(AttendanceRecord.member_id == Member.id, AttendanceRecord.event_id == event_id)
        .exists()
    )
    group_ids = db.scalars(
        select(RelationshipMembership.group_id)
        .join(Member, Member.id == RelationshipMembership.member_id)
        .where(Member.status.is_(True))
        .group_by(RelationshipMembership.group_id)
        .having(func.sum(case((checked, 1), else_=0)) > 0)
        .having(func.sum(case((checked, 0), else_=1)) > 0)
        .order_by(RelationshipMembership.group_id)
        .offset(offset)
        .limit(limit)
    ).all()
    if not group_ids:
        return RelationshipGroupList(items=[])

    rows = db.execute(
        select(RelationshipMembership.group_id, Member)
        .join(Member, Member.id == RelationshipMembership.member_id)
        .where(RelationshipMembership.group_id.in_(group_ids), Member.status.is_(True))
        .order_by(Member.name, Member.id)
    ).all()
    member_ids = [member.id for _, member in rows]
    records = db.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.event_id == event_id, AttendanceRecord.member_id.in_(member_ids),
        )
    ).all()
    attendance = {record.member_id: record for record in records}
    groups = {group_id: RelationshipGroupOut(id=group_id, members=[]) for group_id in group_ids}
    for group_id, member in rows:
        out = _to_member_out(member)
        record = attendance.get(member.id)
        out.attendance_status = "checked_in" if record else "not_checked_in"
        out.attendance_record_id = record.id if record else None
        out.attendance_event_id = event_id
        groups[group_id].members.append(out)
    return RelationshipGroupList(items=list(groups.values()))


def _relationship_group_out(member_id: UUID, db: Session) -> RelationshipGroupOut:
    if db.get(Member, member_id) is None:
        raise HTTPException(status_code=404, detail="member not found")
    membership = db.get(RelationshipMembership, member_id)
    if membership is None:
        return RelationshipGroupOut(id=None, members=[])
    members = db.scalars(
        select(Member)
        .join(RelationshipMembership, RelationshipMembership.member_id == Member.id)
        .where(RelationshipMembership.group_id == membership.group_id)
        .order_by(Member.name, Member.id)
    ).all()
    return RelationshipGroupOut(id=membership.group_id, members=[_to_member_out(member) for member in members])


@router.get("/{member_id}/relationship-group", response_model=RelationshipGroupOut)
def get_relationship_group(member_id: UUID, db: Session = Depends(get_db)) -> RelationshipGroupOut:
    return _relationship_group_out(member_id, db)


@router.put("/{member_id}/relationship-group", response_model=RelationshipGroupOut)
def update_relationship_group(
    member_id: UUID, payload: RelationshipGroupUpdate, db: Session = Depends(get_db),
) -> RelationshipGroupOut:
    requested = set(payload.member_ids)
    if member_id not in requested or len(requested) != len(payload.member_ids):
        raise HTTPException(status_code=422, detail="成员列表必须包含本人且不能重复")
    members = db.scalars(select(Member).where(Member.id.in_(requested))).all()
    if len(members) != len(requested):
        raise HTTPException(status_code=404, detail="会友不存在，请刷新后重试")

    anchor = db.get(RelationshipMembership, member_id)
    group_id = anchor.group_id if anchor else None
    current = db.scalars(
        select(RelationshipMembership).where(RelationshipMembership.group_id == group_id)
    ).all() if group_id is not None else []
    current_ids = {membership.member_id for membership in current} if anchor else {member_id}
    if payload.expected_member_ids is not None and (
        payload.expected_group_id != group_id or set(payload.expected_member_ids) != current_ids
    ):
        raise HTTPException(status_code=409, detail="关系组已发生变化，请刷新后重试")
    memberships = db.scalars(
        select(RelationshipMembership).where(RelationshipMembership.member_id.in_(requested))
    ).all()
    transfers = [m for m in memberships if m.group_id != group_id]
    actual_transfers = {(m.member_id, m.group_id) for m in transfers}
    confirmed = {(m.member_id, m.group_id) for m in payload.confirmed_transfers}
    if transfers and (not payload.allow_transfer or confirmed != actual_transfers):
        names = {member.id: member.name for member in members}
        raise HTTPException(
            status_code=409,
            detail={
                "code": "transfer_required",
                "message": "以下会友已属于其他关系组，确认后将转入本组："
                + "、".join(f"{names[m.member_id]} ({m.member_id})" for m in transfers),
                "transfers": [
                    {"member_id": str(m.member_id), "group_id": str(m.group_id)} for m in transfers
                ],
            },
        )
    if payload.allow_transfer and confirmed != actual_transfers:
        raise HTTPException(status_code=409, detail="关系组已发生变化，请刷新后重试")
    if group_id is None:
        group = RelationshipGroup()
        db.add(group)
        db.flush()
        group_id = group.id
    for membership in current:
        if membership.member_id not in requested:
            db.delete(membership)
    existing = {membership.member_id: membership for membership in memberships}
    for requested_id in requested:
        membership = existing.get(requested_id)
        if membership is None:
            db.add(RelationshipMembership(member_id=requested_id, group_id=group_id))
        else:
            membership.group_id = group_id
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="关系组已发生变化，请刷新后重试")
    return _relationship_group_out(member_id, db)


@router.delete("/{member_id}/relationship-group", response_model=RelationshipGroupOut)
def leave_relationship_group(member_id: UUID, db: Session = Depends(get_db)) -> RelationshipGroupOut:
    result = _relationship_group_out(member_id, db)
    if result.id is not None:
        membership = db.get(RelationshipMembership, member_id)
        if membership is not None:
            db.delete(membership)
            db.commit()
    return RelationshipGroupOut(id=None, members=[])


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
    birthday_fields = ("birthday", "birthday_month", "birthday_day", "age_at_record")
    merged = {
        field: getattr(payload, field) if field in provided_fields else getattr(member, field)
        for field in birthday_fields
    }
    _validate_birthday_fields(**{
        "birthday": merged["birthday"],
        "month": merged["birthday_month"],
        "day": merged["birthday_day"],
        "age": merged["age_at_record"],
    })

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
    if "birthday_month" in provided_fields:
        member.birthday_month = payload.birthday_month
    if "birthday_day" in provided_fields:
        member.birthday_day = payload.birthday_day
    if "age_at_record" in provided_fields:
        if payload.age_at_record is None:
            member.age_recorded_on = None
        elif payload.age_at_record != member.age_at_record or member.age_recorded_on is None:
            member.age_recorded_on = today_local()
        member.age_at_record = payload.age_at_record
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
