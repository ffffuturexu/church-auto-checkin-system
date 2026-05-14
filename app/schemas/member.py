from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel


class MemberOut(BaseModel):
    id: UUID
    name: str
    name_chn: str | None
    age: int | None
    has_photo: bool
    gender: str | None
    group: str | None
    birthday: date | None
    note: str | None
    status: bool
    attendance_status: str | None = None
    attendance_record_id: UUID | None = None
    attendance_event_id: UUID | None = None


class MemberSearchResponse(BaseModel):
    total: int
    items: list[MemberOut]


class MemberCreateRequest(BaseModel):
    name: str
    name_chn: str | None = None
    gender: str | None = None
    group: str | None = None
    birthday: date | None = None
    note: str | None = None
    status: bool = True


class MemberUpdateRequest(BaseModel):
    name: str | None = None
    name_chn: str | None = None
    gender: str | None = None
    group: str | None = None
    birthday: date | None = None
    note: str | None = None
    status: bool | None = None


class MemberListResponse(BaseModel):
    total: int
    items: list[MemberOut]
