from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class FacePhotoOut(BaseModel):
    id: UUID
    member_id: UUID
    local_path: str
    original_filename: str
    mime_type: str | None
    remote_face_id: str | None
    is_active: bool
    created_at: datetime


class FacePhotoListResponse(BaseModel):
    total: int
    items: list[FacePhotoOut]


class FacePhotoDeleteResponse(BaseModel):
    status: str
    photo_id: UUID


class FacePhotoUploadResponse(BaseModel):
    status: str
    photo: FacePhotoOut


class FaceLibrarySyncSummary(BaseModel):
    processed: int
    synced: int
    failed: int
    missing_files: int


class FaceLibrarySyncResponse(BaseModel):
    status: str
    summary: FaceLibrarySyncSummary
