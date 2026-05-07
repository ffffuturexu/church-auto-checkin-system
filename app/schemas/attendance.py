from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class ManualCheckInRequest(BaseModel):
    member_id: UUID
    event_id: UUID | None = None
    event_date: date | None = None
    service_language: Literal["auto", "zh", "it"] | None = "auto"


class AttendanceRecordOut(BaseModel):
    id: UUID
    event_id: UUID
    member_id: UUID
    member_name: str
    check_in_time: str
    method: str


class ManualCheckInResponse(BaseModel):
    status: str
    record: AttendanceRecordOut


class AttendanceHistoryResponse(BaseModel):
    total: int
    items: list[AttendanceRecordOut]


class AttendanceRecordDeleteResponse(BaseModel):
    status: str
    record: AttendanceRecordOut


class AttendanceDashboardSummary(BaseModel):
    active_members: int
    active_events: int
    total_checkins: int
    auto_checkins: int
    manual_checkins: int
    pending_unknown_cases: int
    avg_this_year_zh: float
    avg_this_year_it: float


class AttendanceDashboardDailyPoint(BaseModel):
    event_date: date
    checkins: int


class AttendanceDashboardEventPoint(BaseModel):
    event_id: UUID
    event_name: str
    event_date: date
    checkins: int


class AttendanceDashboardResponse(BaseModel):
    summary: AttendanceDashboardSummary
    daily: list[AttendanceDashboardDailyPoint]
    top_events: list[AttendanceDashboardEventPoint]


class CurrentServiceInfoResponse(BaseModel):
    date: date
    is_sunday: bool
    event_id: UUID | None = None
    service_language: Literal["zh", "it"] | None = None
    service_title: str | None = None
    event_name: str | None = None
    display_text: str
