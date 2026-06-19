from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.service_event import get_or_create_sunday_service_event, is_sunday
from app.core.time_utils import parse_timestamp_to_local_naive
from app.core.websocket_manager import WebSocketManager
from app.models.models import (
    AttendanceEvent,
    AttendanceRecord,
    CheckInMethod,
    Member,
    RecognitionLog,
    RecognitionStatus,
    UnknownCaseStatus,
    UnknownFaceCase,
)
from app.services.runtime_pipeline import RuntimePipeline
from app.services.reception_feed_service import record_feed_event


class EventDispatcher:
    """Consume recognition events, persist to DB, and broadcast by websocket channel."""

    def __init__(
        self,
        runtime: RuntimePipeline,
        ws_manager: WebSocketManager,
        poll_interval_sec: float = 0.05,
    ) -> None:
        self.runtime = runtime
        self.ws_manager = ws_manager
        self.poll_interval_sec = poll_interval_sec

        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="event-dispatcher")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            event = self.runtime.read_event_nowait()
            if event is None:
                await asyncio.sleep(self.poll_interval_sec)
                continue
            await self._handle_event(event)

    async def _handle_event(self, event: dict) -> None:
        event_type = str(event.get("event_type", ""))

        if event_type in {"check_in", "recognition.success"}:
            normalized = self._normalize_success_event(event)
            persisted_payload = self._persist_checkin_event(normalized)
            if persisted_payload is not None:
                await self.ws_manager.broadcast_channel_a(persisted_payload)
            return

        if event_type == "recognition.pending":
            persisted_payload = self._persist_pending_event(event)
            if persisted_payload is not None:
                await self.ws_manager.broadcast_channel_a(persisted_payload)
            return

        if event_type == "recognition_log":
            self._persist_recognition_log_event(event)
            return

        if event_type == "debug_frame":
            await self.ws_manager.broadcast_channel_b(event)
            return

        if event_type in {"unknown_face", "recognition.unknown"}:
            persisted_payload = self._persist_unknown_event(event)
            if persisted_payload is not None:
                await self.ws_manager.broadcast_channel_a(persisted_payload)
            return

        # Other runtime events go to channel A for ops visibility.
        await self.ws_manager.broadcast_channel_a(event)

    @staticmethod
    def _normalize_success_event(event: dict) -> dict:
        if str(event.get("event_type", "")) == "check_in":
            return dict(event)

        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        payload = {
            "event_type": "check_in",
            "event_id": event.get("event_id"),
            "timestamp": event.get("timestamp"),
            "method": data.get("method") or "auto_face",
            "subject_id": str(data.get("subject_id") or "").strip(),
            "similarity": data.get("best_similarity", data.get("similarity")),
            "best_similarity": data.get("best_similarity", data.get("similarity")),
            "second_subject_id": data.get("second_subject_id"),
            "second_similarity": data.get("second_similarity"),
            "face_image_base64": data.get("face_image_base64"),
            "box": data.get("box"),
            "camera_id": event.get("camera_id"),
            "recognition_event_type": event.get("event_type"),
            "recognition_data": data,
        }
        return payload

    @staticmethod
    def _extract_business_data(event: dict) -> dict:
        if str(event.get("event_type", "")).startswith("recognition.") and isinstance(event.get("data"), dict):
            return dict(event.get("data") or {})
        return dict(event)

    @classmethod
    def _extract_subject_id_for_event(cls, event: dict) -> str | None:
        data = cls._extract_business_data(event)
        value = str(data.get("subject_id") or data.get("best_subject_id") or "").strip()
        return value or None

    @classmethod
    def _extract_similarity_for_event(cls, event: dict):
        data = cls._extract_business_data(event)
        return data.get("best_similarity", data.get("similarity"))

    @classmethod
    def _extract_image_base64_for_event(cls, event: dict) -> str:
        data = cls._extract_business_data(event)
        return str(data.get("face_image_base64") or data.get("image_base64") or event.get("image_base64") or "")

    def _persist_checkin_event(self, event: dict) -> dict:
        payload = dict(event)
        subject_id = str(event.get("subject_id", "")).strip()
        timestamp = self._parse_timestamp(event.get("timestamp"))

        with SessionLocal() as db:
            member = self._find_member_by_subject(db, subject_id)
            if member is None:
                orphaned_payload = self._persist_orphaned_checkin_as_unknown(
                    db=db,
                    event=event,
                    timestamp=timestamp,
                    subject_id=subject_id,
                )
                if orphaned_payload.get("event_type") in {"check_in", "check_in_ignored"}:
                    record_feed_event(db, orphaned_payload, source="auto_face")
                db.commit()
                return orphaned_payload

            if not is_sunday(timestamp.date()):
                ignored_payload = self._build_ignored_checkin_payload(
                    event=payload,
                    timestamp=timestamp,
                    member=member,
                    reason="non_sunday",
                    detail="auto check-in is only persisted on Sunday",
                )
                record_feed_event(db, ignored_payload, source="auto_face")
                db.commit()
                return ignored_payload

            event_row = get_or_create_sunday_service_event(db, checkin_time_local=timestamp)
            existing = db.scalar(
                select(AttendanceRecord).where(
                    AttendanceRecord.event_id == event_row.id,
                    AttendanceRecord.member_id == member.id,
                )
            )
            if existing is not None:
                ignored_payload = self._build_ignored_checkin_payload(
                    event=payload,
                    timestamp=timestamp,
                    member=member,
                    reason="already_checked_in",
                    detail="member already checked in for this event",
                    event_id=event_row.id,
                    attendance_record_id=existing.id,
                )
                record_feed_event(db, ignored_payload, source="auto_face")
                db.commit()
                return ignored_payload

            record = AttendanceRecord(
                event_id=event_row.id,
                member_id=member.id,
                check_in_time=timestamp,
                method=CheckInMethod.AUTO_FACE,
            )
            db.add(record)
            db.flush()

            payload["persist_status"] = "ok"
            payload["member_id"] = str(member.id)
            payload["member_name"] = member.name
            payload["member_name_chn"] = member.name_chn
            payload["event_id"] = str(event_row.id)
            payload["attendance_record_id"] = str(record.id)
            source_event_type = str(event.get("recognition_event_type") or event.get("event_type") or "")
            payload["event_type"] = "recognition.success" if source_event_type == "recognition.success" else "check_in"
            payload["data"] = {
                "decision": "success",
                "queue_kind": None,
                "method": payload.get("method") or "auto_face",
                "subject_id": subject_id,
                "subject_name": member.name,
                "similarity": self._safe_float(payload.get("similarity")),
                "best_similarity": self._safe_float(payload.get("best_similarity") or payload.get("similarity")),
                "second_subject_id": payload.get("second_subject_id"),
                "second_similarity": self._safe_float(payload.get("second_similarity")),
                "reason": None,
                "face_image_base64": None,
                "box": payload.get("box"),
            }
            # Legacy compatibility fields for existing front-end logic.
            payload["legacy_event_type"] = "check_in"
            payload.pop("face_image_base64", None)
            payload.pop("image_base64", None)
            record_feed_event(db, payload, source="auto_face")
            db.commit()
            return payload

    @staticmethod
    def _build_ignored_checkin_payload(
        event: dict,
        timestamp: datetime,
        member: Member,
        reason: str,
        detail: str,
        event_id=None,
        attendance_record_id=None,
    ) -> dict:
        payload = dict(event)
        payload["event_type"] = "check_in_ignored"
        payload["timestamp"] = timestamp.isoformat(timespec="seconds")
        payload["persist_status"] = reason
        payload["detail"] = detail
        payload["member_id"] = str(member.id)
        payload["member_name"] = member.name
        payload["member_name_chn"] = member.name_chn
        if event_id is not None:
            payload["event_id"] = str(event_id)
        if attendance_record_id is not None:
            payload["attendance_record_id"] = str(attendance_record_id)
        # Ensure large base64 image fields are not sent over Channel A
        payload.pop("face_image_base64", None)
        payload.pop("image_base64", None)
        return payload

    def _persist_orphaned_checkin_as_unknown(
        self,
        db,
        event: dict,
        timestamp: datetime,
        subject_id: str,
    ) -> dict:
        image_base64 = str(event.get("face_image_base64") or event.get("image_base64") or "")
        best_subject_id = subject_id or "unknown"
        best_subject_name = self._resolve_member_name_by_subject(db, best_subject_id)
        best_subject_name_chn = self._resolve_member_name_chn_by_subject(db, best_subject_id)
        note = self._build_unknown_note(
            base_note=(f"profile_not_found:{subject_id}" if subject_id else "profile_not_found"),
            best_subject_id=best_subject_id,
            best_subject_name=best_subject_name,
            best_subject_name_chn=best_subject_name_chn,
            second_subject_id=None,
            second_subject_name=None,
        )

        self._create_recognition_log(
            db,
            status=RecognitionStatus.UNKNOWN,
            best_subject_id=best_subject_id,
            similarity=event.get("similarity"),
            second_subject_id=None,
            second_similarity=None,
            timestamp=timestamp,
        )

        row = UnknownFaceCase(
            timestamp=timestamp,
            reason="orphaned_profile",
            image_base64=image_base64,
            status=UnknownCaseStatus.PENDING,
            note=note,
        )
        db.add(row)
        db.flush()

        similarity = self._safe_float(event.get("similarity"))
        source_event_type = str(event.get("recognition_event_type") or event.get("event_type") or "")
        payload = {
            "event_type": "recognition.pending" if source_event_type == "recognition.success" else "unknown_face",
            "queue_kind": "pending_review",
            "timestamp": timestamp.isoformat(timespec="seconds"),
            "reason": "orphaned_profile",
            "persist_status": "profile_not_found",
            "subject_id": subject_id,
            "best_subject_id": best_subject_id,
            "best_subject_name": best_subject_name,
            "best_subject_name_chn": best_subject_name_chn,
            "similarity": similarity,
            "case_id": str(row.id),
            "queue_status": UnknownCaseStatus.PENDING.value,
            "note": row.note,
            "legacy_event_type": "unknown_face",
            "data": {
                "decision": "pending",
                "queue_kind": "pending_review",
                "method": "pending_review",
                "subject_id": best_subject_id,
                "subject_name": best_subject_name,
                "similarity": similarity,
                "best_similarity": similarity,
                "second_subject_id": None,
                "second_similarity": None,
                "reason": "orphaned_profile",
                "face_image_base64": None,
                "box": event.get("box"),
            },
        }
        # Do not include the base64 image in websocket payloads for Channel A
        # to avoid large strings polluting the reception/check-in views.
        return payload

    def _persist_recognition_log_event(self, event: dict) -> None:
        status = self._safe_status(event.get("status"))
        with SessionLocal() as db:
            self._create_recognition_log(
                db,
                status=status,
                best_subject_id=str(event.get("best_subject_id", "unknown")),
                similarity=event.get("similarity"),
                second_subject_id=event.get("second_subject_id"),
                second_similarity=event.get("second_similarity"),
                timestamp=self._parse_timestamp(event.get("timestamp")),
            )
            db.commit()

    def _persist_pending_event(self, event: dict) -> dict | None:
        return self._persist_queue_event(event, decision="pending")

    def _persist_unknown_event(self, event: dict) -> dict | None:
        return self._persist_queue_event(event, decision="unknown")

    def _persist_queue_event(self, event: dict, decision: str) -> dict | None:
        payload = dict(event)
        data = self._extract_business_data(event)
        timestamp = self._parse_timestamp(event.get("timestamp"))
        reason = (str(data.get("reason") or event.get("reason") or "").strip() or None)
        queue_kind = str(data.get("queue_kind") or event.get("queue_kind") or "").strip() or None
        if queue_kind is None:
            queue_kind = "pending_review" if decision == "pending" else "stranger"

        best_subject_id = str(data.get("subject_id") or data.get("best_subject_id") or "").strip() or None
        second_subject_id = str(data.get("second_subject_id") or "").strip() or None
        image_base64 = self._extract_image_base64_for_event(event)
        similarity = self._safe_float(data.get("best_similarity", data.get("similarity")))
        second_similarity = self._safe_float(data.get("second_similarity"))

        with SessionLocal() as db:
            if decision == "unknown" and self._should_suppress_unknown_after_successful_checkin(
                db,
                event=event,
                timestamp=timestamp,
            ):
                return None

            best_subject_name = self._resolve_member_name_by_subject(db, best_subject_id)
            best_subject_name_chn = self._resolve_member_name_chn_by_subject(db, best_subject_id)
            second_subject_name = self._resolve_member_name_by_subject(db, second_subject_id)
            note = self._build_unknown_note(
                base_note=(str(data.get("note") or event.get("note") or "").strip() or None),
                best_subject_id=best_subject_id,
                best_subject_name=best_subject_name,
                best_subject_name_chn=best_subject_name_chn,
                second_subject_id=second_subject_id,
                second_subject_name=second_subject_name,
            )

            # Avoid creating duplicate RecognitionLog rows when a recognition_log
            # with the same timestamp and subject pair already exists (some
            # workflows emit a recognition_log followed by an unknown_face payload
            # for the same detection). In that case, reuse the existing log.
            best_subject_id_str = best_subject_id or "unknown"
            second_subject_id_str = second_subject_id or None
            existing_log = db.scalar(
                select(RecognitionLog).where(
                    RecognitionLog.timestamp == timestamp,
                    RecognitionLog.best_subject_id == best_subject_id_str,
                    RecognitionLog.second_subject_id == (second_subject_id_str),
                )
            )

            if existing_log is None:
                if decision == "pending":
                    self._create_recognition_log(
                        db,
                        status=RecognitionStatus.PENDING,
                        best_subject_id=best_subject_id_str,
                        similarity=similarity,
                        second_subject_id=second_subject_id_str,
                        second_similarity=second_similarity,
                        timestamp=timestamp,
                    )
                else:
                    self._create_recognition_log(
                        db,
                        status=RecognitionStatus.UNKNOWN,
                        best_subject_id=best_subject_id_str,
                        similarity=similarity,
                        second_subject_id=second_subject_id_str,
                        second_similarity=second_similarity,
                        timestamp=timestamp,
                    )

            row = UnknownFaceCase(
                timestamp=timestamp,
                reason=reason,
                image_base64=image_base64,
                status=UnknownCaseStatus.PENDING,
                note=note,
            )
            db.add(row)
            db.commit()

            source_event_type = str(event.get("event_type") or "")
            if source_event_type == "unknown_face":
                payload["event_type"] = "unknown_face"
            else:
                payload["event_type"] = f"recognition.{decision}"
            payload["case_id"] = str(row.id)
            payload["queue_status"] = UnknownCaseStatus.PENDING.value
            payload["queue_kind"] = queue_kind
            payload["best_subject_id"] = best_subject_id
            payload["best_subject_name"] = best_subject_name
            payload["best_subject_name_chn"] = best_subject_name_chn
            payload["second_subject_id"] = second_subject_id
            payload["second_subject_name"] = second_subject_name
            payload["reason"] = reason
            payload["note"] = row.note
            payload["legacy_event_type"] = "unknown_face"
            payload["data"] = {
                "decision": decision,
                "queue_kind": queue_kind,
                "method": data.get("method") or ("pending_review" if decision == "pending" else "stranger_detected"),
                "subject_id": best_subject_id if decision == "pending" else None,
                "subject_name": best_subject_name if decision == "pending" else None,
                "similarity": similarity,
                "best_similarity": similarity,
                "second_subject_id": second_subject_id,
                "second_similarity": second_similarity,
                "reason": reason,
                "face_image_base64": None,
                "box": data.get("box"),
            }
            payload.pop("image_base64", None)
            payload.pop("face_image_base64", None)
            return payload

    def _should_suppress_unknown_after_successful_checkin(
        self,
        db,
        event: dict,
        timestamp: datetime,
    ) -> bool:
        best_subject_id = self._extract_subject_id_for_event(event) or ""
        if not best_subject_id:
            return False

        member = self._find_member_by_subject(db, best_subject_id)
        if member is None:
            return False

        existing = db.scalar(
            select(AttendanceRecord.id)
            .join(AttendanceEvent, AttendanceEvent.id == AttendanceRecord.event_id)
            .where(
                AttendanceRecord.member_id == member.id,
                AttendanceEvent.event_date == timestamp.date(),
            )
        )
        return existing is not None

    @staticmethod
    def _find_member_by_subject(db, subject_id: str) -> Member | None:
        if not subject_id:
            return None
        try:
            subject_uuid = uuid.UUID(subject_id)
        except ValueError:
            return None

        stmt = select(Member).where(
            Member.id == subject_uuid,
            Member.status.is_(True),
        )
        return db.scalar(stmt)

    @staticmethod
    def _create_recognition_log(
        db,
        status: RecognitionStatus,
        best_subject_id: str,
        similarity,
        second_subject_id,
        second_similarity,
        timestamp: datetime,
    ) -> None:
        best_subject_id_str = str(best_subject_id) if best_subject_id else "unknown"
        second_subject_id_str = str(second_subject_id) if second_subject_id else None

        row = RecognitionLog(
            timestamp=timestamp,
            best_subject_id=best_subject_id_str,
            similarity=EventDispatcher._safe_float(similarity),
            second_subject_id=second_subject_id_str,
            second_similarity=EventDispatcher._safe_float(second_similarity),
            best_subject_name=EventDispatcher._resolve_member_name_by_subject(db, best_subject_id_str),
            second_subject_name=EventDispatcher._resolve_member_name_by_subject(db, second_subject_id_str),
            status=status,
        )
        db.add(row)

    @staticmethod
    def _resolve_member_name_by_subject(db, subject_id: str | None) -> str | None:
        if not subject_id:
            return None

        try:
            subject_uuid = uuid.UUID(subject_id)
        except ValueError:
            return subject_id

        stmt = select(Member.name).where(Member.id == subject_uuid)
        member_name = db.scalar(stmt)
        if member_name:
            return str(member_name)
        return subject_id

    @staticmethod
    def _resolve_member_name_chn_by_subject(db, subject_id: str | None) -> str | None:
        if not subject_id:
            return None

        try:
            subject_uuid = uuid.UUID(subject_id)
        except ValueError:
            return None

        stmt = select(Member.name_chn).where(Member.id == subject_uuid)
        member_name_chn = db.scalar(stmt)
        if member_name_chn:
            return str(member_name_chn)
        return None

    @staticmethod
    def _build_unknown_note(
        base_note: str | None,
        best_subject_id: str | None,
        best_subject_name: str | None,
        best_subject_name_chn: str | None,
        second_subject_id: str | None,
        second_subject_name: str | None,
    ) -> str | None:
        parts: list[str] = []
        if best_subject_id:
            parts.append(f"best_subject_id={best_subject_id}")
        if best_subject_name:
            parts.append(f"best_subject_name={best_subject_name}")
        if best_subject_name_chn:
            parts.append(f"best_subject_name_chn={best_subject_name_chn}")
        if second_subject_id:
            parts.append(f"second_subject_id={second_subject_id}")
        if second_subject_name:
            parts.append(f"second_subject_name={second_subject_name}")

        metadata = ";".join(parts)
        text = ""
        if base_note:
            text = base_note
        if metadata:
            if text:
                text = f"{text} | {metadata}"
            else:
                text = metadata

        if not text:
            return None
        return text[:255]

    @staticmethod
    def _parse_timestamp(value) -> datetime:
        return parse_timestamp_to_local_naive(value)

    @staticmethod
    def _safe_float(value):
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_status(value) -> RecognitionStatus:
        if isinstance(value, RecognitionStatus):
            return value
        try:
            return RecognitionStatus(str(value))
        except ValueError:
            pass

        try:
            return RecognitionStatus[str(value).split(".")[-1].upper()]
        except (KeyError, AttributeError):
            return RecognitionStatus.UNKNOWN
