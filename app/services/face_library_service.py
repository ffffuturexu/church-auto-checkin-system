from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import HTTPException
import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import CompreFaceClient
from app.models.models import FacePhoto, Member


class FaceLibraryService:
    def __init__(self, client: CompreFaceClient, storage_root: str) -> None:
        self.client = client
        self.storage_root = storage_root
        os.makedirs(self.storage_root, exist_ok=True)

    def list_member_photos(self, db: Session, member_id: uuid.UUID, active_only: bool = True) -> list[FacePhoto]:
        stmt = select(FacePhoto).where(FacePhoto.member_id == member_id).order_by(FacePhoto.created_at.desc())
        if active_only:
            stmt = stmt.where(FacePhoto.is_active.is_(True))
        return db.execute(stmt).scalars().all()

    def create_photo(
        self,
        db: Session,
        member_id: uuid.UUID,
        file_bytes: bytes,
        filename: str,
        mime_type: str | None,
    ) -> FacePhoto:
        member = db.get(Member, member_id)
        if member is None or not member.status:
            raise HTTPException(status_code=404, detail="member not found or inactive")

        subject_name = str(member.id)
        try:
            self.client.create_subject(subject_name)
        except requests.exceptions.HTTPError as exc:
            raise self._http_exception_from_compreface(exc, action="create_subject") from exc
        except requests.exceptions.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"CompreFace request failed while creating subject: {exc}") from exc

        extension = Path(filename or "").suffix.lower() or ".jpg"
        photo_filename = f"{uuid.uuid4()}{extension}"
        relative_path = os.path.join(subject_name, photo_filename)
        absolute_path = os.path.join(self.storage_root, relative_path)
        os.makedirs(os.path.dirname(absolute_path), exist_ok=True)

        with open(absolute_path, "wb") as f:
            f.write(file_bytes)

        try:
            payload = self.client.add_face_image(subject_name, absolute_path)
            remote_face_id = self._extract_remote_face_id(payload)

            row = FacePhoto(
                member_id=member.id,
                local_path=relative_path,
                original_filename=filename or photo_filename,
                mime_type=mime_type,
                remote_face_id=remote_face_id,
                is_active=True,
            )
            db.add(row)
            member.has_photo = True
            db.commit()
            db.refresh(row)
            return row
        except requests.exceptions.HTTPError as exc:
            try:
                os.remove(absolute_path)
            except OSError:
                pass
            db.rollback()
            raise self._http_exception_from_compreface(exc, action="add_face_image") from exc
        except requests.exceptions.RequestException as exc:
            try:
                os.remove(absolute_path)
            except OSError:
                pass
            db.rollback()
            raise HTTPException(status_code=502, detail=f"CompreFace request failed while uploading photo: {exc}") from exc
        except Exception:
            try:
                os.remove(absolute_path)
            except OSError:
                pass
            db.rollback()
            raise

    def replace_photo(
        self,
        db: Session,
        photo_id: uuid.UUID,
        file_bytes: bytes,
        filename: str,
        mime_type: str | None,
    ) -> FacePhoto:
        row = db.get(FacePhoto, photo_id)
        if row is None or not row.is_active:
            raise HTTPException(status_code=404, detail="photo not found")

        member = db.get(Member, row.member_id)
        if member is None:
            raise HTTPException(status_code=404, detail="member not found")

        old_relative_path = row.local_path
        old_abs = os.path.join(self.storage_root, old_relative_path)

        # Best-effort remove previous remote face if we have an explicit face id.
        if row.remote_face_id:
            try:
                self.client.delete_face(row.remote_face_id)
            except Exception:
                pass

        extension = Path(filename or "").suffix.lower() or ".jpg"
        new_filename = f"{uuid.uuid4()}{extension}"
        relative_path = os.path.join(str(member.id), new_filename)
        absolute_path = os.path.join(self.storage_root, relative_path)
        os.makedirs(os.path.dirname(absolute_path), exist_ok=True)

        with open(absolute_path, "wb") as f:
            f.write(file_bytes)

        row.local_path = relative_path
        row.original_filename = filename or new_filename
        row.mime_type = mime_type

        try:
            # Rebuild remote subject from active local photos so remote state matches SQLite.
            self._resync_member_remote(db, member.id)

            if old_relative_path != relative_path:
                try:
                    os.remove(old_abs)
                except OSError:
                    pass

            db.commit()
            db.refresh(row)
            return row
        except Exception:
            try:
                os.remove(absolute_path)
            except OSError:
                pass
            db.rollback()
            raise

    def delete_photo(self, db: Session, photo_id: uuid.UUID) -> FacePhoto:
        row = db.get(FacePhoto, photo_id)
        if row is None or not row.is_active:
            raise HTTPException(status_code=404, detail="photo not found")

        member = db.get(Member, row.member_id)
        if member is None:
            raise HTTPException(status_code=404, detail="member not found")

        row.is_active = False

        if row.remote_face_id:
            try:
                self.client.delete_face(row.remote_face_id)
            except Exception:
                pass

        absolute_path = os.path.join(self.storage_root, row.local_path)
        try:
            os.remove(absolute_path)
        except OSError:
            pass

        self._refresh_member_has_photo(db, member.id)

        db.commit()
        db.refresh(row)
        return row

    def resync_photos_to_remote(
        self,
        db: Session,
        member_id: uuid.UUID | None = None,
        active_only: bool = True,
    ) -> dict[str, int]:
        stmt = (
            select(FacePhoto, Member)
            .join(Member, FacePhoto.member_id == Member.id)
            .order_by(FacePhoto.created_at.asc())
        )

        if member_id is not None:
            stmt = stmt.where(FacePhoto.member_id == member_id)

        if active_only:
            stmt = stmt.where(
                FacePhoto.is_active.is_(True),
                Member.status.is_(True),
            )

        rows = db.execute(stmt).all()
        photos_by_member: dict[uuid.UUID, list[FacePhoto]] = {}
        for photo, member in rows:
            bucket = photos_by_member.get(member.id)
            if bucket is None:
                photos_by_member[member.id] = [photo]
            else:
                bucket.append(photo)

        stats = {
            "processed": 0,
            "synced": 0,
            "failed": 0,
            "missing_files": 0,
        }

        for current_member_id, photos in photos_by_member.items():
            subject_name = str(current_member_id)
            try:
                # Rebuild remote subject every sync to avoid duplicate faces on repeated sync calls.
                self.client.delete_subject(subject_name)
            except Exception:
                pass

            try:
                self.client.create_subject(subject_name)
            except Exception:
                pass

            for photo in photos:
                stats["processed"] += 1
                absolute_path = os.path.join(self.storage_root, photo.local_path)
                if not os.path.exists(absolute_path):
                    photo.remote_face_id = None
                    stats["missing_files"] += 1
                    continue

                try:
                    payload = self.client.add_face_image(subject_name, absolute_path)
                    photo.remote_face_id = self._extract_remote_face_id(payload)
                    stats["synced"] += 1
                except Exception:
                    stats["failed"] += 1
                    photo.remote_face_id = None

        db.commit()
        return stats

    @staticmethod
    def _extract_remote_face_id(payload: dict | None) -> str | None:
        if not isinstance(payload, dict):
            return None

        for key in ("face_id", "id"):
            value = payload.get(key)
            if value:
                return str(value)

        result = payload.get("result")
        if isinstance(result, list) and result:
            top = result[0]
            if isinstance(top, dict):
                for key in ("face_id", "id"):
                    value = top.get(key)
                    if value:
                        return str(value)

        if isinstance(result, dict):
            for key in ("face_id", "id"):
                value = result.get(key)
                if value:
                    return str(value)
        return None

    @staticmethod
    def _http_exception_from_compreface(
        exc: requests.exceptions.HTTPError,
        action: str,
    ) -> HTTPException:
        response = exc.response
        upstream_status = response.status_code if response is not None else None

        message = "unknown error"
        code = None
        if response is not None:
            try:
                payload = response.json()
            except ValueError:
                payload = None

            if isinstance(payload, dict):
                raw_message = payload.get("message")
                if raw_message:
                    message = str(raw_message)
                raw_code = payload.get("code")
                if raw_code is not None:
                    code = str(raw_code)
            else:
                text = (response.text or "").strip()
                if text:
                    message = text

        if upstream_status == 400:
            status_code = 422
        elif upstream_status in (401, 403):
            status_code = 502
        elif upstream_status is not None and upstream_status >= 500:
            status_code = 502
        else:
            status_code = 500

        detail = f"CompreFace {action} failed: {message}"
        if code is not None:
            detail = f"{detail} (code={code})"

        return HTTPException(status_code=status_code, detail=detail)

    def _resync_member_remote(self, db: Session, member_id: uuid.UUID) -> None:
        subject_name = str(member_id)

        try:
            self.client.delete_subject(subject_name)
        except Exception:
            pass
        self.client.create_subject(subject_name)

        stmt = (
            select(FacePhoto)
            .where(FacePhoto.member_id == member_id, FacePhoto.is_active.is_(True))
            .order_by(FacePhoto.created_at.asc())
        )
        photos = db.scalars(stmt).all()

        for photo in photos:
            absolute_path = os.path.join(self.storage_root, photo.local_path)
            if not os.path.exists(absolute_path):
                photo.remote_face_id = None
                continue
            payload = self.client.add_face_image(subject_name, absolute_path)
            photo.remote_face_id = self._extract_remote_face_id(payload)

    def _refresh_member_has_photo(self, db: Session, member_id: uuid.UUID) -> None:
        db.flush()
        member = db.get(Member, member_id)
        if member is None:
            return

        active_count = db.scalar(
            select(func.count(FacePhoto.id)).where(
                FacePhoto.member_id == member_id,
                FacePhoto.is_active.is_(True),
            )
        )
        member.has_photo = bool(active_count)
