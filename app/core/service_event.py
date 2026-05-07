from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import AttendanceEvent

SUNDAY_SERVICE_LANGUAGE_ZH = "zh"
SUNDAY_SERVICE_LANGUAGE_IT = "it"


def is_sunday(target_date: date) -> bool:
    return target_date.weekday() == 6


def is_first_sunday_of_month(target_date: date) -> bool:
    return is_sunday(target_date) and 1 <= target_date.day <= 7


def normalize_service_language(language: str | None) -> str:
    value = (language or "").strip().lower()
    if value in {"it", "italian", "意语", "意大利"}:
        return SUNDAY_SERVICE_LANGUAGE_IT
    return SUNDAY_SERVICE_LANGUAGE_ZH


def parse_service_language_override(language: str | None) -> str | None:
    value = (language or "").strip().lower()
    if value in {"", "auto", "自动"}:
        return None
    return normalize_service_language(value)


def infer_sunday_service_language(checkin_time_local: datetime) -> str:
    target_date = checkin_time_local.date()
    if is_first_sunday_of_month(target_date):
        # First Sunday has only Chinese service.
        return SUNDAY_SERVICE_LANGUAGE_ZH

    split = time(hour=15, minute=0, second=0)
    if checkin_time_local.time() < split:
        return SUNDAY_SERVICE_LANGUAGE_ZH
    return SUNDAY_SERVICE_LANGUAGE_IT


def sunday_service_event_name(target_date: date, language: str) -> str:
    title = sunday_service_title(language)
    return f"{title} {target_date.isoformat()}"


def sunday_service_title(language: str) -> str:
    normalized = normalize_service_language(language)
    label = "中文" if normalized == SUNDAY_SERVICE_LANGUAGE_ZH else "意语"
    return f"主日崇拜（{label}）"


def get_or_create_sunday_service_event(
    db: Session,
    checkin_time_local: datetime,
    *,
    target_date: date | None = None,
    language: str | None = None,
) -> AttendanceEvent:
    event_date = target_date or checkin_time_local.date()
    if not is_sunday(event_date):
        raise ValueError("non-sunday date cannot create sunday service event")

    manual_language = parse_service_language_override(language)

    if is_first_sunday_of_month(event_date):
        final_language = SUNDAY_SERVICE_LANGUAGE_ZH
    elif manual_language is not None:
        final_language = manual_language
    else:
        final_language = infer_sunday_service_language(
            datetime.combine(event_date, checkin_time_local.time())
        )

    event_name = sunday_service_event_name(event_date, final_language)
    row = db.scalar(
        select(AttendanceEvent).where(
            AttendanceEvent.event_date == event_date,
            AttendanceEvent.event_name == event_name,
            AttendanceEvent.is_archived.is_(False),
        )
    )
    if row is not None:
        return row

    row = AttendanceEvent(
        event_name=event_name,
        event_date=event_date,
        is_archived=False,
    )
    db.add(row)
    db.flush()
    return row
