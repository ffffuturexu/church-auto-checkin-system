from __future__ import annotations

from datetime import datetime
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class UnknownFaceCaseOut(BaseModel):
    id: UUID
    timestamp: datetime
    reason: str | None
    image_base64: str
    best_subject_id: str | None = None
    best_subject_name: str | None = None
    best_subject_name_chn: str | None = None
    second_subject_id: str | None = None
    second_subject_name: str | None = None
    status: str
    member_id: UUID | None
    event_id: UUID | None
    attendance_record_id: UUID | None
    note: str | None
    handled_at: datetime | None


class UnknownFaceCaseListResponse(BaseModel):
    total: int
    items: list[UnknownFaceCaseOut]


class UnknownFaceCaseActionResponse(BaseModel):
    status: str
    case: UnknownFaceCaseOut
    message: str | None = None


class UnknownFaceResolveRequest(BaseModel):
    member_id: UUID
    event_id: UUID | None = None
    event_date: date | None = None
    service_language: Literal["auto", "zh", "it"] | None = "auto"
    note: str | None = None


class UnknownFaceIgnoreRequest(BaseModel):
    note: str | None = None


class UnknownFaceClearResponse(BaseModel):
    status: str
    cleared: int
