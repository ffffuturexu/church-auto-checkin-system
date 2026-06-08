from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel


class CareMemberListItem(BaseModel):
    member_id: UUID
    name: str
    name_chn: str | None
    group: str | None
    gender: str | None
    status: bool
    has_photo: bool
    recent_checkins: int
    last_checkin_date: date | None
    days_since_last_checkin: int | None
    sunday_absent_streak: int
    trend_delta_30_vs_prev60: int
    risk_level: str
    risk_score: int


class CareMemberListResponse(BaseModel):
    total: int
    items: list[CareMemberListItem]


class CareMemberSummary(BaseModel):
    recent_checkins: int
    last_checkin_date: date | None
    days_since_last_checkin: int | None
    sunday_absent_streak: int
    trend_delta_30_vs_prev60: int
    risk_level: str
    risk_score: int


class CareRecentRecordItem(BaseModel):
    record_id: UUID
    event_id: UUID
    event_name: str
    event_date: date
    check_in_time: str
    method: str


class CareMonthlyBreakdownItem(BaseModel):
    month: str
    checkins: int


class CareMemberProfileResponse(BaseModel):
    member: CareMemberListItem
    summary: CareMemberSummary
    recent_records: list[CareRecentRecordItem]
    monthly_breakdown: list[CareMonthlyBreakdownItem]


class CareCohortItem(BaseModel):
    cohort_key: str
    title: str
    description: str
    count: int
    suggested_filters: dict[str, str]


class CareCohortResponse(BaseModel):
    items: list[CareCohortItem]


class CareReportSummary(BaseModel):
    total_members: int
    active_members: int
    needs_followup: int
    high_risk: int
    medium_risk: int
    low_risk: int
    no_photo_active: int
    unassigned_active: int


class CareDistributionItem(BaseModel):
    key: str
    label: str
    count: int


class CareReportResponse(BaseModel):
    summary: CareReportSummary
    group_distribution: list[CareDistributionItem]
    risk_distribution: list[CareDistributionItem]
    engagement_distribution: list[CareDistributionItem]


class CareUpcomingBirthdayItem(BaseModel):
    member_id: UUID
    name: str
    name_chn: str | None
    group: str | None
    birthday: date
    next_birthday: date
    days_until_birthday: int
    turning_age: int


class CareUpcomingBirthdayResponse(BaseModel):
    total: int
    days_window: int
    items: list[CareUpcomingBirthdayItem]
