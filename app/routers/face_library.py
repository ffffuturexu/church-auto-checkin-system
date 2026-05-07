from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api import CompreFaceClient
from app.core.config import settings as core_settings
from app.core.database import get_db
from app.models.models import FacePhoto
from app.schemas.face_library import (
    FacePhotoDeleteResponse,
    FacePhotoListResponse,
    FacePhotoOut,
    FaceLibrarySyncResponse,
    FacePhotoUploadResponse,
)
from app.services.face_library_service import FaceLibraryService

router = APIRouter(prefix="/face-library", tags=["face-library"])


def _resolve_storage_root(request: Request | None = None) -> str:
    if request is not None:
        resolved = getattr(request.app.state, "face_storage_dir", None)
        if isinstance(resolved, str) and resolved.strip():
            return resolved

    root = Path(core_settings.face_storage_dir)
    if not root.is_absolute():
        root = (Path(__file__).resolve().parents[2] / root).resolve()
    return str(root)


def _to_photo_out(row: FacePhoto) -> FacePhotoOut:
    return FacePhotoOut(
        id=row.id,
        member_id=row.member_id,
        local_path=row.local_path,
        original_filename=row.original_filename,
        mime_type=row.mime_type,
        remote_face_id=row.remote_face_id,
        is_active=row.is_active,
        created_at=row.created_at,
    )


def _get_service(request: Request) -> FaceLibraryService:
    service = getattr(request.app.state, "face_library_service", None)
    if service is not None:
        return service
    return FaceLibraryService(CompreFaceClient(), _resolve_storage_root(request))


@router.get("/members/{member_id}/photos", response_model=FacePhotoListResponse)
def list_member_photos(
    member_id: UUID,
    request: Request,
    active_only: bool = True,
    db: Session = Depends(get_db),
) -> FacePhotoListResponse:
    service = _get_service(request)
    rows = service.list_member_photos(db, member_id=member_id, active_only=active_only)
    items = [_to_photo_out(photo) for photo in rows]
    return FacePhotoListResponse(total=len(items), items=items)


@router.post("/members/{member_id}/photos", response_model=FacePhotoUploadResponse)
async def upload_member_photo(
    member_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> FacePhotoUploadResponse:
    service = _get_service(request)
    data = await file.read()
    row = service.create_photo(
        db,
        member_id=member_id,
        file_bytes=data,
        filename=file.filename or "",
        mime_type=file.content_type,
    )
    return FacePhotoUploadResponse(status="ok", photo=_to_photo_out(row))


@router.put("/photos/{photo_id}", response_model=FacePhotoUploadResponse)
async def replace_member_photo(
    photo_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> FacePhotoUploadResponse:
    service = _get_service(request)
    data = await file.read()
    row = service.replace_photo(
        db,
        photo_id=photo_id,
        file_bytes=data,
        filename=file.filename or "",
        mime_type=file.content_type,
    )
    return FacePhotoUploadResponse(status="ok", photo=_to_photo_out(row))


@router.delete("/photos/{photo_id}", response_model=FacePhotoDeleteResponse)
def delete_member_photo(
    photo_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> FacePhotoDeleteResponse:
    service = _get_service(request)
    row = service.delete_photo(db, photo_id=photo_id)
    return FacePhotoDeleteResponse(status="deleted", photo_id=row.id)


@router.get("/photos/{photo_id}/download")
def download_photo(photo_id: UUID, request: Request, db: Session = Depends(get_db)) -> FileResponse:
    row = db.get(FacePhoto, photo_id)
    if row is None:
        raise HTTPException(status_code=404, detail="photo not found")

    service = _get_service(request)
    absolute_path = os.path.join(service.storage_root, row.local_path)
    if not os.path.exists(absolute_path):
        raise HTTPException(status_code=404, detail="photo file not found")

    return FileResponse(
        absolute_path,
        media_type=row.mime_type or "application/octet-stream",
        filename=row.original_filename,
    )


@router.post("/sync/rebuild", response_model=FaceLibrarySyncResponse)
@router.post("/sync", response_model=FaceLibrarySyncResponse)
@router.post("/rebuild-sync", response_model=FaceLibrarySyncResponse)
def rebuild_face_library_sync(
    request: Request,
    member_id: UUID | None = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
) -> FaceLibrarySyncResponse:
    service = _get_service(request)
    summary = service.resync_photos_to_remote(
        db,
        member_id=member_id,
        active_only=active_only,
    )
    return FaceLibrarySyncResponse(status="ok", summary=summary)
