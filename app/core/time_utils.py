from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.core.config import settings

APP_ZONE = ZoneInfo(settings.app_timezone)


def now_local() -> datetime:
    return datetime.now(APP_ZONE)


def now_local_naive() -> datetime:
    return now_local().replace(tzinfo=None)


def today_local() -> date:
    return now_local().date()


def to_local_naive(value: datetime) -> datetime:
    # If timestamp is naive (no tzinfo), treat it as already in APP_ZONE
    # (the application historically stored local naive datetimes).
    if value.tzinfo is None:
        source = value.replace(tzinfo=APP_ZONE)
    else:
        source = value.astimezone(APP_ZONE)
    return source.replace(tzinfo=None)


def to_local_aware(value: datetime) -> datetime:
    # If timestamp is naive (no tzinfo), treat it as already in APP_ZONE
    if value.tzinfo is None:
        source = value.replace(tzinfo=APP_ZONE)
    else:
        source = value.astimezone(APP_ZONE)
    return source


def parse_timestamp_to_local_naive(value) -> datetime:
    if isinstance(value, datetime):
        return to_local_naive(value)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return to_local_naive(parsed)
        except ValueError:
            pass
    return now_local_naive()
