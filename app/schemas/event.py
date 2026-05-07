from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel


class AttendanceEventOut(BaseModel):
    id: UUID
    event_name: str
    event_date: date
    is_archived: bool


class AttendanceEventListResponse(BaseModel):
    total: int
    items: list[AttendanceEventOut]


class AttendanceEventCreateRequest(BaseModel):
    event_name: str
    event_date: date


class AttendanceEventUpdateRequest(BaseModel):
    event_name: str | None = None
    event_date: date | None = None


class AttendanceEventArchiveRequest(BaseModel):
    is_archived: bool
