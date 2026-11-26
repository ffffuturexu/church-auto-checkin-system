from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field, StrictInt


class MemberOut(BaseModel):
    id: UUID
    name: str
    name_chn: str | None
    age: int | None
    has_photo: bool
    gender: str | None
    group: str | None
    birthday: date | None
    birthday_month: int | None = None
    birthday_day: int | None = None
    age_at_record: int | None = None
    age_recorded_on: date | None = None
    note: str | None
    status: bool
    attendance_status: str | None = None
    attendance_record_id: UUID | None = None
    attendance_event_id: UUID | None = None


class MemberSearchResponse(BaseModel):
    total: int
    items: list[MemberOut]


class ExpandedSearchMember(BaseModel):
    member: MemberOut
    direct_match: bool


class ExpandedSearchGroup(BaseModel):
    group_id: UUID | None
    members: list[ExpandedSearchMember]


class ExpandedSearchResponse(BaseModel):
    groups: list[ExpandedSearchGroup]
    truncated: bool


class RelationshipGroupOut(BaseModel):
    id: UUID | None
    members: list[MemberOut]


class RelationshipTransfer(BaseModel):
    member_id: UUID
    group_id: UUID


class RelationshipGroupUpdate(BaseModel):
    member_ids: list[UUID]
    allow_transfer: bool = False
    expected_group_id: UUID | None = None
    expected_member_ids: list[UUID] | None = None
    confirmed_transfers: list[RelationshipTransfer] = Field(default_factory=list)


class RelationshipGroupList(BaseModel):
    items: list[RelationshipGroupOut]


class MemberCreateRequest(BaseModel):
    id: UUID | None = None
    name: str
    name_chn: str | None = None
    gender: str | None = None
    group: str | None = None
    birthday: date | None = None
    birthday_month: StrictInt | None = Field(default=None, ge=1, le=12)
    birthday_day: StrictInt | None = Field(default=None, ge=1, le=31)
    age_at_record: StrictInt | None = Field(default=None, ge=0, le=130)
    note: str | None = None
    status: bool = True


class MemberUpdateRequest(BaseModel):
    name: str | None = None
    name_chn: str | None = None
    gender: str | None = None
    group: str | None = None
    birthday: date | None = None
    birthday_month: StrictInt | None = Field(default=None, ge=1, le=12)
    birthday_day: StrictInt | None = Field(default=None, ge=1, le=31)
    age_at_record: StrictInt | None = Field(default=None, ge=0, le=130)
    note: str | None = None
    status: bool | None = None


class MemberListResponse(BaseModel):
    total: int
    items: list[MemberOut]
