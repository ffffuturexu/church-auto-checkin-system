from __future__ import annotations

import csv
import io
import re
from calendar import isleap
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.time_utils import now_local, to_local_aware, today_local
from app.models.models import AttendanceEvent, AttendanceRecord, Member
from app.schemas.care import (
    CareCohortItem,
    CareCohortResponse,
    CareDistributionItem,
    CareMemberListItem,
    CareMemberListResponse,
    CareMemberProfileResponse,
    CareMemberSummary,
    CareMonthlyBreakdownItem,
    CareUpcomingBirthdayItem,
    CareUpcomingBirthdayResponse,
    CareRecentRecordItem,
    CareReportResponse,
    CareReportSummary,
)

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # pragma: no cover - optional dependency fallback
    Style = None
    lazy_pinyin = None

router = APIRouter(prefix="/care", tags=["care"])


@dataclass
class _MemberMetrics:
    recent_checkins: int = 0
    last_checkin_date: date | None = None
    days_since_last_checkin: int | None = None
    sunday_absent_streak: int = 0
    trend_delta_30_vs_prev60: int = 0
    risk_level: str = "low"
    risk_score: int = 0


def _latest_sunday(ref_date: date) -> date:
    days_since_sunday = (ref_date.weekday() + 1) % 7
    return ref_date - timedelta(days=days_since_sunday)


def _recent_sundays(today: date, weeks: int) -> list[date]:
    latest = _latest_sunday(today)
    return [latest - timedelta(days=7 * idx) for idx in range(weeks)]


def _months_window_start(ref_date: date, months_window: int) -> date:
    """Return the first day of the earliest calendar month in the window."""
    months_back = max(1, months_window) - 1
    year = ref_date.year
    month = ref_date.month - months_back
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _safe_birthday_this_year(birthday: date, year: int) -> date:
    if birthday.month == 2 and birthday.day == 29 and not isleap(year):
        return date(year, 2, 28)
    return date(year, birthday.month, birthday.day)


def _next_birthday_date(birthday: date, ref_date: date) -> date:
    candidate = _safe_birthday_this_year(birthday, ref_date.year)
    if candidate < ref_date:
        candidate = _safe_birthday_this_year(birthday, ref_date.year + 1)
    return candidate


def _normalize_gender_value(gender: str | None) -> str:
    value = (gender or "").strip().lower()
    if value in {"male", "m", "man", "男"}:
        return "男"
    if value in {"female", "f", "woman", "女"}:
        return "女"
    return value


def _normalize_search_text(value: str | None) -> str:
    return re.sub(r"\s+", "", (value or "").strip().lower())


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
            compact = _normalize_search_text(candidate) if candidate else ""
            if initials and acronym == initials.lower():
                return True
            if pinyin_initials and acronym == pinyin_initials.lower():
                return True
            if pinyin_full and acronym in pinyin_full:
                return True
            if compact and acronym in compact:
                return True

    return False


def _risk_from_metrics(
    *,
    recent_checkins: int,
    days_since_last: int | None,
    sunday_absent_streak: int,
    trend_delta: int,
    has_photo: bool,
    status: bool,
) -> tuple[int, str]:
    score = 0

    if recent_checkins <= 1:
        score += 35
    elif recent_checkins <= 3:
        score += 20

    if days_since_last is None:
        score += 40
    elif days_since_last > 90:
        score += 30
    elif days_since_last > 60:
        score += 20
    elif days_since_last > 30:
        score += 10

    if sunday_absent_streak >= 8:
        score += 25
    elif sunday_absent_streak >= 4:
        score += 15
    elif sunday_absent_streak >= 2:
        score += 8

    if trend_delta < 0:
        score += min(15, abs(trend_delta) * 5)

    if status and not has_photo:
        score += 8

    score = max(0, min(100, score))
    if score >= 70:
        return score, "high"
    if score >= 40:
        return score, "medium"
    return score, "low"


def _event_scope_filter(only_sunday: bool):
    if not only_sunday:
        return None
    return func.strftime("%w", AttendanceEvent.event_date) == "0"


def _base_member_query(
    *,
    status_filter: str,
    has_photo_filter: str,
    gender: str | None,
    group_filter: str | None,
) -> Select:
    stmt: Select = select(Member)

    if status_filter == "active":
        stmt = stmt.where(Member.status.is_(True))
    elif status_filter == "inactive":
        stmt = stmt.where(Member.status.is_(False))

    if has_photo_filter == "with_photo":
        stmt = stmt.where(Member.has_photo.is_(True))
    elif has_photo_filter == "without_photo":
        stmt = stmt.where(Member.has_photo.is_(False))

    normalized_gender = _normalize_gender_value(gender)
    if normalized_gender:
        stmt = stmt.where(Member.gender.in_([normalized_gender, normalized_gender.lower()]))

    normalized_group = (group_filter or "").strip()
    if normalized_group:
        if normalized_group == "__empty__":
            stmt = stmt.where((Member.group.is_(None)) | (func.trim(Member.group) == ""))
        else:
            stmt = stmt.where(Member.group == normalized_group)

    return stmt.order_by(Member.name.asc())


def _collect_metrics(
    *,
    db: Session,
    members: list[Member],
    months_window: int,
    only_sunday: bool,
    sunday_streak_weeks: int = 16,
) -> dict[UUID, _MemberMetrics]:
    metrics = {member.id: _MemberMetrics() for member in members}
    if not members:
        return metrics

    today = today_local()
    member_ids = [member.id for member in members]
    recent_start = _months_window_start(today, months_window)
    trend_recent_start = today - timedelta(days=30)
    trend_prev_start = today - timedelta(days=90)

    event_scope = _event_scope_filter(only_sunday)

    last_stmt = (
        select(AttendanceRecord.member_id, func.max(AttendanceEvent.event_date))
        .join(AttendanceEvent, AttendanceRecord.event_id == AttendanceEvent.id)
        .where(AttendanceRecord.member_id.in_(member_ids))
        .group_by(AttendanceRecord.member_id)
    )
    if event_scope is not None:
        last_stmt = last_stmt.where(event_scope)

    for member_id, last_date in db.execute(last_stmt).all():
        if member_id not in metrics:
            continue
        metrics[member_id].last_checkin_date = last_date
        if last_date is not None:
            metrics[member_id].days_since_last_checkin = (today - last_date).days

    recent_stmt = (
        select(AttendanceRecord.member_id, AttendanceEvent.event_date)
        .join(AttendanceEvent, AttendanceRecord.event_id == AttendanceEvent.id)
        .where(
            AttendanceRecord.member_id.in_(member_ids),
            AttendanceEvent.event_date >= recent_start,
        )
    )
    if event_scope is not None:
        recent_stmt = recent_stmt.where(event_scope)

    for member_id, event_date in db.execute(recent_stmt).all():
        if member_id not in metrics:
            continue
        metrics[member_id].recent_checkins += 1

    trend_stmt = (
        select(AttendanceRecord.member_id, AttendanceEvent.event_date)
        .join(AttendanceEvent, AttendanceRecord.event_id == AttendanceEvent.id)
        .where(
            AttendanceRecord.member_id.in_(member_ids),
            AttendanceEvent.event_date >= trend_prev_start,
        )
    )
    if event_scope is not None:
        trend_stmt = trend_stmt.where(event_scope)

    for member_id, event_date in db.execute(trend_stmt).all():
        if member_id not in metrics:
            continue
        if event_date >= trend_recent_start:
            metrics[member_id].trend_delta_30_vs_prev60 += 1
        else:
            metrics[member_id].trend_delta_30_vs_prev60 -= 1

    sundays = _recent_sundays(today, sunday_streak_weeks)
    sunday_set = set(sundays)
    sunday_checkins: dict[UUID, set[date]] = defaultdict(set)

    sunday_stmt = (
        select(AttendanceRecord.member_id, AttendanceEvent.event_date)
        .join(AttendanceEvent, AttendanceRecord.event_id == AttendanceEvent.id)
        .where(
            AttendanceRecord.member_id.in_(member_ids),
            AttendanceEvent.event_date.in_(sunday_set),
            func.strftime("%w", AttendanceEvent.event_date) == "0",
        )
    )
    for member_id, event_date in db.execute(sunday_stmt).all():
        sunday_checkins[member_id].add(event_date)

    for member in members:
        streak = 0
        checked = sunday_checkins.get(member.id, set())
        for sunday in sundays:
            if sunday in checked:
                break
            streak += 1
        metrics[member.id].sunday_absent_streak = streak

        score, level = _risk_from_metrics(
            recent_checkins=metrics[member.id].recent_checkins,
            days_since_last=metrics[member.id].days_since_last_checkin,
            sunday_absent_streak=metrics[member.id].sunday_absent_streak,
            trend_delta=metrics[member.id].trend_delta_30_vs_prev60,
            has_photo=member.has_photo,
            status=member.status,
        )
        metrics[member.id].risk_score = score
        metrics[member.id].risk_level = level

    return metrics


def _to_care_item(member: Member, m: _MemberMetrics) -> CareMemberListItem:
    return CareMemberListItem(
        member_id=member.id,
        name=member.name,
        name_chn=member.name_chn,
        group=member.group,
        gender=member.gender,
        status=member.status,
        has_photo=member.has_photo,
        recent_checkins=m.recent_checkins,
        last_checkin_date=m.last_checkin_date,
        days_since_last_checkin=m.days_since_last_checkin,
        sunday_absent_streak=m.sunday_absent_streak,
        trend_delta_30_vs_prev60=m.trend_delta_30_vs_prev60,
        risk_level=m.risk_level,
        risk_score=m.risk_score,
    )


def _cohort_match(item: CareMemberListItem, cohort_key: str | None) -> bool:
    key = (cohort_key or "").strip()
    if not key:
        return True

    group_text = (item.group or "").strip()
    if key == "newcomer_active":
        return item.status and ("新人" in group_text) and item.recent_checkins >= 3
    if key == "low_attendance_recent":
        return item.status and item.recent_checkins <= 1
    if key == "declining_engagement":
        return item.status and item.trend_delta_30_vs_prev60 < 0 and item.recent_checkins <= 3
    if key == "active_but_unassigned":
        return item.status and item.recent_checkins >= 2 and not group_text
    if key == "no_photo_active":
        return item.status and (not item.has_photo)
    return True


def _filter_items(
    items: list[CareMemberListItem],
    *,
    min_checkins: int,
    max_checkins: int | None,
    min_days_since_last_checkin: int | None,
    max_days_since_last_checkin: int | None,
    cohort_key: str | None,
) -> list[CareMemberListItem]:
    out: list[CareMemberListItem] = []
    for item in items:
        if item.recent_checkins < min_checkins:
            continue
        if max_checkins is not None and item.recent_checkins > max_checkins:
            continue

        if min_days_since_last_checkin is not None:
            if item.days_since_last_checkin is None or item.days_since_last_checkin < min_days_since_last_checkin:
                continue

        if max_days_since_last_checkin is not None:
            if item.days_since_last_checkin is None or item.days_since_last_checkin > max_days_since_last_checkin:
                continue

        if not _cohort_match(item, cohort_key):
            continue

        out.append(item)
    return out


def _sort_items(items: list[CareMemberListItem], sort_by: str, sort_dir: str) -> list[CareMemberListItem]:
    reverse = sort_dir == "desc"

    def key_func(item: CareMemberListItem):
        if sort_by == "risk_score":
            return (item.risk_score, item.days_since_last_checkin or -1, item.name.lower())
        if sort_by == "last_checkin":
            date_ord = item.last_checkin_date.toordinal() if item.last_checkin_date else -1
            return (date_ord, item.recent_checkins, item.name.lower())
        if sort_by == "checkins":
            return (item.recent_checkins, item.days_since_last_checkin or -1, item.name.lower())
        return (item.name.lower(),)

    return sorted(items, key=key_func, reverse=reverse)


def _build_items(
    *,
    db: Session,
    months_window: int,
    name_keyword: str | None,
    min_checkins: int,
    max_checkins: int | None,
    status_filter: str,
    has_photo_filter: str,
    gender: str | None,
    group_filter: str | None,
    only_sunday: bool,
    min_days_since_last_checkin: int | None,
    max_days_since_last_checkin: int | None,
    cohort_key: str | None,
    sort_by: str,
    sort_dir: str,
) -> list[CareMemberListItem]:
    members = db.execute(
        _base_member_query(
            status_filter=status_filter,
            has_photo_filter=has_photo_filter,
            gender=gender,
            group_filter=group_filter,
        )
    ).scalars().all()

    if name_keyword:
        members = [member for member in members if _matches_member_keyword(member, name_keyword)]

    member_metrics = _collect_metrics(
        db=db,
        members=members,
        months_window=months_window,
        only_sunday=only_sunday,
    )

    items = [_to_care_item(member, member_metrics[member.id]) for member in members]
    items = _filter_items(
        items,
        min_checkins=min_checkins,
        max_checkins=max_checkins,
        min_days_since_last_checkin=min_days_since_last_checkin,
        max_days_since_last_checkin=max_days_since_last_checkin,
        cohort_key=cohort_key,
    )
    return _sort_items(items, sort_by=sort_by, sort_dir=sort_dir)


@router.get("/members", response_model=CareMemberListResponse)
def list_care_members(
    months_window: int = Query(default=3, ge=1, le=24),
    name_keyword: str | None = Query(default=None, max_length=120),
    min_checkins: int = Query(default=0, ge=0, le=500),
    max_checkins: int | None = Query(default=None, ge=0, le=500),
    status_filter: str = Query(default="active", pattern="^(all|active|inactive)$"),
    has_photo_filter: str = Query(default="all", pattern="^(all|with_photo|without_photo)$"),
    gender: str | None = Query(default=None, max_length=16),
    group_filter: str | None = Query(default=None, max_length=120),
    only_sunday: bool = Query(default=True),
    min_days_since_last_checkin: int | None = Query(default=None, ge=0, le=5000),
    max_days_since_last_checkin: int | None = Query(default=None, ge=0, le=5000),
    cohort_key: str | None = Query(default=None, max_length=60),
    sort_by: str = Query(default="risk_score", pattern="^(risk_score|last_checkin|checkins|name)$"),
    sort_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=80, ge=1, le=500),
    db: Session = Depends(get_db),
) -> CareMemberListResponse:
    items = _build_items(
        db=db,
        months_window=months_window,
        name_keyword=name_keyword,
        min_checkins=min_checkins,
        max_checkins=max_checkins,
        status_filter=status_filter,
        has_photo_filter=has_photo_filter,
        gender=gender,
        group_filter=group_filter,
        only_sunday=only_sunday,
        min_days_since_last_checkin=min_days_since_last_checkin,
        max_days_since_last_checkin=max_days_since_last_checkin,
        cohort_key=cohort_key,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )

    total = len(items)
    page = items[offset : offset + limit]
    return CareMemberListResponse(total=total, items=page)


@router.get("/members/{member_id}/profile", response_model=CareMemberProfileResponse)
def care_member_profile(
    member_id: UUID,
    months_window: int = Query(default=6, ge=1, le=24),
    only_sunday: bool = Query(default=True),
    recent_records_limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> CareMemberProfileResponse:
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="member not found")

    metrics = _collect_metrics(
        db=db,
        members=[member],
        months_window=months_window,
        only_sunday=only_sunday,
    )[member.id]
    item = _to_care_item(member, metrics)

    record_stmt = (
        select(AttendanceRecord, AttendanceEvent)
        .join(AttendanceEvent, AttendanceRecord.event_id == AttendanceEvent.id)
        .where(AttendanceRecord.member_id == member_id)
    )
    event_scope = _event_scope_filter(only_sunday)
    if event_scope is not None:
        record_stmt = record_stmt.where(event_scope)
    record_stmt = record_stmt.order_by(AttendanceEvent.event_date.desc(), AttendanceRecord.check_in_time.desc())

    rows = db.execute(record_stmt.limit(recent_records_limit)).all()
    recent_records: list[CareRecentRecordItem] = []
    for record, event in rows:
        local_dt = to_local_aware(record.check_in_time).strftime("%Y-%m-%d %H:%M:%S")
        recent_records.append(
            CareRecentRecordItem(
                record_id=record.id,
                event_id=event.id,
                event_name=event.event_name,
                event_date=event.event_date,
                check_in_time=local_dt,
                method=record.method.value,
            )
        )

    month_start = _months_window_start(today_local(), months_window)
    month_rows_stmt = (
        select(AttendanceEvent.event_date)
        .join(AttendanceRecord, AttendanceRecord.event_id == AttendanceEvent.id)
        .where(
            AttendanceRecord.member_id == member_id,
            AttendanceEvent.event_date >= month_start,
        )
    )
    if event_scope is not None:
        month_rows_stmt = month_rows_stmt.where(event_scope)

    month_counter: Counter[str] = Counter()
    for (event_date,) in db.execute(month_rows_stmt).all():
        month_counter[event_date.strftime("%Y-%m")] += 1

    monthly_breakdown = [
        CareMonthlyBreakdownItem(month=month, checkins=month_counter[month])
        for month in sorted(month_counter.keys(), reverse=True)
    ]

    summary = CareMemberSummary(
        recent_checkins=item.recent_checkins,
        last_checkin_date=item.last_checkin_date,
        days_since_last_checkin=item.days_since_last_checkin,
        sunday_absent_streak=item.sunday_absent_streak,
        trend_delta_30_vs_prev60=item.trend_delta_30_vs_prev60,
        risk_level=item.risk_level,
        risk_score=item.risk_score,
    )
    return CareMemberProfileResponse(
        member=item,
        summary=summary,
        recent_records=recent_records,
        monthly_breakdown=monthly_breakdown,
    )


@router.get("/cohorts", response_model=CareCohortResponse)
def care_cohorts(
    months_window: int = Query(default=3, ge=1, le=24),
    only_sunday: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> CareCohortResponse:
    items = _build_items(
        db=db,
        months_window=months_window,
        name_keyword=None,
        min_checkins=0,
        max_checkins=None,
        status_filter="active",
        has_photo_filter="all",
        gender=None,
        group_filter=None,
        only_sunday=only_sunday,
        min_days_since_last_checkin=None,
        max_days_since_last_checkin=None,
        cohort_key=None,
        sort_by="risk_score",
        sort_dir="desc",
    )

    definitions = [
        (
            "newcomer_active",
            "新人且积极参与",
            "分组包含新人，且最近参与稳定",
            {"cohort_key": "newcomer_active", "status_filter": "active"},
        ),
        (
            "low_attendance_recent",
            "近期低出席",
            "最近窗口内签到较少，建议重点关怀",
            {"cohort_key": "low_attendance_recent", "status_filter": "active"},
        ),
        (
            "declining_engagement",
            "参与度下滑",
            "近30天较前60天出现明显下滑",
            {"cohort_key": "declining_engagement", "status_filter": "active"},
        ),
        (
            "active_but_unassigned",
            "活跃但未分组",
            "近期参与不错，但尚未进入稳定分组",
            {"cohort_key": "active_but_unassigned", "status_filter": "active"},
        ),
        (
            "no_photo_active",
            "活跃但缺少照片",
            "有参与但未完成照片资料",
            {"cohort_key": "no_photo_active", "status_filter": "active"},
        ),
    ]

    cohort_items: list[CareCohortItem] = []
    for key, title, description, suggested_filters in definitions:
        count = sum(1 for item in items if _cohort_match(item, key))
        cohort_items.append(
            CareCohortItem(
                cohort_key=key,
                title=title,
                description=description,
                count=count,
                suggested_filters=suggested_filters,
            )
        )

    return CareCohortResponse(items=cohort_items)


@router.get("/report", response_model=CareReportResponse)
def care_report(
    months_window: int = Query(default=3, ge=1, le=24),
    only_sunday: bool = Query(default=True),
    status_filter: str = Query(default="active", pattern="^(all|active|inactive)$"),
    db: Session = Depends(get_db),
) -> CareReportResponse:
    items = _build_items(
        db=db,
        months_window=months_window,
        name_keyword=None,
        min_checkins=0,
        max_checkins=None,
        status_filter=status_filter,
        has_photo_filter="all",
        gender=None,
        group_filter=None,
        only_sunday=only_sunday,
        min_days_since_last_checkin=None,
        max_days_since_last_checkin=None,
        cohort_key=None,
        sort_by="risk_score",
        sort_dir="desc",
    )

    total_members = len(items)
    active_members = sum(1 for item in items if item.status)
    high_risk = sum(1 for item in items if item.risk_level == "high")
    medium_risk = sum(1 for item in items if item.risk_level == "medium")
    low_risk = sum(1 for item in items if item.risk_level == "low")
    needs_followup = high_risk + medium_risk
    no_photo_active = sum(1 for item in items if item.status and not item.has_photo)
    unassigned_active = sum(1 for item in items if item.status and not (item.group or "").strip())

    group_counter: Counter[str] = Counter()
    for item in items:
        group_name = (item.group or "").strip() or "(未分组)"
        group_counter[group_name] += 1

    group_distribution = [
        CareDistributionItem(key=group_name, label=group_name, count=count)
        for group_name, count in sorted(group_counter.items(), key=lambda pair: (-pair[1], pair[0]))
    ]

    risk_distribution = [
        CareDistributionItem(key="high", label="高风险", count=high_risk),
        CareDistributionItem(key="medium", label="中风险", count=medium_risk),
        CareDistributionItem(key="low", label="低风险", count=low_risk),
    ]

    engagement_buckets = {
        "inactive": 0,
        "low": 0,
        "steady": 0,
        "engaged": 0,
    }
    for item in items:
        if item.recent_checkins <= 0:
            engagement_buckets["inactive"] += 1
        elif item.recent_checkins <= 2:
            engagement_buckets["low"] += 1
        elif item.recent_checkins <= 5:
            engagement_buckets["steady"] += 1
        else:
            engagement_buckets["engaged"] += 1

    engagement_distribution = [
        CareDistributionItem(key="inactive", label="未参与", count=engagement_buckets["inactive"]),
        CareDistributionItem(key="low", label="低参与", count=engagement_buckets["low"]),
        CareDistributionItem(key="steady", label="稳定参与", count=engagement_buckets["steady"]),
        CareDistributionItem(key="engaged", label="高参与", count=engagement_buckets["engaged"]),
    ]

    summary = CareReportSummary(
        total_members=total_members,
        active_members=active_members,
        needs_followup=needs_followup,
        high_risk=high_risk,
        medium_risk=medium_risk,
        low_risk=low_risk,
        no_photo_active=no_photo_active,
        unassigned_active=unassigned_active,
    )
    return CareReportResponse(
        summary=summary,
        group_distribution=group_distribution,
        risk_distribution=risk_distribution,
        engagement_distribution=engagement_distribution,
    )


@router.get("/members/export.csv")
def export_care_members_csv(
    months_window: int = Query(default=3, ge=1, le=24),
    name_keyword: str | None = Query(default=None, max_length=120),
    min_checkins: int = Query(default=0, ge=0, le=500),
    max_checkins: int | None = Query(default=None, ge=0, le=500),
    status_filter: str = Query(default="active", pattern="^(all|active|inactive)$"),
    has_photo_filter: str = Query(default="all", pattern="^(all|with_photo|without_photo)$"),
    gender: str | None = Query(default=None, max_length=16),
    group_filter: str | None = Query(default=None, max_length=120),
    only_sunday: bool = Query(default=True),
    min_days_since_last_checkin: int | None = Query(default=None, ge=0, le=5000),
    max_days_since_last_checkin: int | None = Query(default=None, ge=0, le=5000),
    cohort_key: str | None = Query(default=None, max_length=60),
    sort_by: str = Query(default="risk_score", pattern="^(risk_score|last_checkin|checkins|name)$"),
    sort_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=5000, ge=1, le=20000),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    items = _build_items(
        db=db,
        months_window=months_window,
        name_keyword=name_keyword,
        min_checkins=min_checkins,
        max_checkins=max_checkins,
        status_filter=status_filter,
        has_photo_filter=has_photo_filter,
        gender=gender,
        group_filter=group_filter,
        only_sunday=only_sunday,
        min_days_since_last_checkin=min_days_since_last_checkin,
        max_days_since_last_checkin=max_days_since_last_checkin,
        cohort_key=cohort_key,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    items = items[:limit]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "会友ID",
            "姓名(拉丁字)",
            "中文名",
            "分组",
            "性别",
            "状态",
            "有照片",
            "近期签到数",
            "最后签到日期",
            "距离最后签到天数",
            "连续主日缺席",
            "趋势(30天-前60天)",
            "风险等级",
            "风险分数",
        ]
    )
    for item in items:
        writer.writerow(
            [
                str(item.member_id),
                item.name,
                item.name_chn or "",
                item.group or "",
                item.gender or "",
                "active" if item.status else "inactive",
                "yes" if item.has_photo else "no",
                item.recent_checkins,
                item.last_checkin_date.isoformat() if item.last_checkin_date else "",
                item.days_since_last_checkin if item.days_since_last_checkin is not None else "",
                item.sunday_absent_streak,
                item.trend_delta_30_vs_prev60,
                item.risk_level,
                item.risk_score,
            ]
        )

    csv_text = buf.getvalue()
    buf.close()
    filename = f"care_members_{now_local().strftime('%Y%m%d_%H%M%S')}.csv"
    filename_ascii = filename
    headers = {
        "Content-Disposition": (
            f"attachment; filename=\"{filename_ascii}\"; "
            f"filename*=UTF-8''{quote(filename)}"
        )
    }
    csv_with_bom = f"\ufeff{csv_text}"
    return StreamingResponse(
        iter([csv_with_bom.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


@router.get("/birthdays", response_model=CareUpcomingBirthdayResponse)
def list_upcoming_birthdays(
    days: int = Query(default=30, ge=1, le=366),
    status_filter: str = Query(default="active", pattern="^(all|active|inactive)$"),
    group_filter: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=300, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> CareUpcomingBirthdayResponse:
    stmt = select(Member).where(Member.birthday.is_not(None))

    if status_filter == "active":
        stmt = stmt.where(Member.status.is_(True))
    elif status_filter == "inactive":
        stmt = stmt.where(Member.status.is_(False))

    normalized_group = (group_filter or "").strip()
    if normalized_group:
        if normalized_group == "__empty__":
            stmt = stmt.where((Member.group.is_(None)) | (func.trim(Member.group) == ""))
        else:
            stmt = stmt.where(Member.group == normalized_group)

    members = db.execute(stmt).scalars().all()
    today = today_local()
    out: list[CareUpcomingBirthdayItem] = []

    for member in members:
        birthday = member.birthday
        if birthday is None:
            continue

        next_birthday = _next_birthday_date(birthday, today)
        days_until = (next_birthday - today).days
        if days_until < 0 or days_until > days:
            continue

        turning_age = next_birthday.year - birthday.year
        out.append(
            CareUpcomingBirthdayItem(
                member_id=member.id,
                name=member.name,
                name_chn=member.name_chn,
                group=member.group,
                birthday=birthday,
                next_birthday=next_birthday,
                days_until_birthday=days_until,
                turning_age=turning_age,
            )
        )

    out.sort(key=lambda item: (item.days_until_birthday, item.next_birthday, item.name.lower()))
    out = out[:limit]

    return CareUpcomingBirthdayResponse(total=len(out), days_window=days, items=out)
