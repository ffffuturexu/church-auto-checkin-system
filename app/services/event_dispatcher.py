from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.service_event import get_or_create_sunday_service_event, is_sunday
from app.core.time_utils import parse_timestamp_to_local_naive
from app.core.websocket_manager import WebSocketManager
from app.models.models import (
    AttendanceEvent,
    AttendanceRecord,
    CheckInMethod,
    CheckInSource,
    Member,
    RecognitionLog,
    RecognitionStatus,
    UnknownCaseStatus,
    UnknownFaceCase,
)
from app.services.runtime_pipeline import RuntimePipeline
from app.services.reception_feed_service import record_feed_event


logger = logging.getLogger(__name__)

PLACEHOLDER_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAAWgmWQ0AAA"
    "AASUVORK5CYII="
)


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
        self._supervisor_task: asyncio.Task | None = None
        self._broadcast_task: asyncio.Task | None = None
        self._debug_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._broadcast_queue: asyncio.Queue[tuple[str, dict, dict | None]] = asyncio.Queue(maxsize=256)
        self._handled = 0
        self._failed_by_type: dict[str, int] = {}
        self._restarts = 0
        self._broadcast_drops_by_type: dict[str, int] = {}
        self._broadcast_failures = 0
        self._broadcast_queue_high_water = 0
        self._broadcast_queue_pressure = False
        self._last_processing_sec = 0.0
        self._max_processing_sec = 0.0
        self._last_broadcast_sec = 0.0
        self._max_broadcast_sec = 0.0

    async def start(self) -> None:
        if self._supervisor_task and not self._supervisor_task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="event-dispatcher")
        self._supervisor_task = asyncio.create_task(self._supervise(), name="event-dispatcher-supervisor")
        self._broadcast_task = asyncio.create_task(self._broadcast_loop(), name="event-broadcast")
        self._debug_task = asyncio.create_task(self._debug_loop(), name="event-debug-broadcast")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._supervisor_task is not None:
            await self._supervisor_task
        if self._broadcast_task is not None:
            try:
                await asyncio.wait_for(self._broadcast_queue.join(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("broadcast shutdown timed out: pending=%d", self._broadcast_queue.qsize())
            self._broadcast_task.cancel()
            await asyncio.gather(self._broadcast_task, return_exceptions=True)
            while not self._broadcast_queue.empty():
                event_type, _, _ = self._broadcast_queue.get_nowait()
                self._record_broadcast_drop(event_type)
                self._broadcast_queue.task_done()
        if self._debug_task is not None:
            self._debug_task.cancel()
            await asyncio.gather(self._debug_task, return_exceptions=True)

    def is_running(self) -> bool:
        return (
            self._task is not None and not self._task.done()
            and self._supervisor_task is not None and not self._supervisor_task.done()
            and self._broadcast_task is not None and not self._broadcast_task.done()
            and self._debug_task is not None and not self._debug_task.done()
        )

    def stats(self) -> dict[str, Any]:
        return {
            "running": self.is_running(),
            "events_handled": self._handled,
            "event_failures_by_type": dict(self._failed_by_type),
            "dispatcher_restarts": self._restarts,
            "broadcast_queue_size": self._broadcast_queue.qsize(),
            "broadcast_queue_max": self._broadcast_queue.maxsize,
            "broadcast_queue_high_water": self._broadcast_queue_high_water,
            "broadcast_drops_by_type": dict(self._broadcast_drops_by_type),
            "broadcast_failures": self._broadcast_failures,
            "last_processing_sec": self._last_processing_sec,
            "max_processing_sec": self._max_processing_sec,
            "last_broadcast_sec": self._last_broadcast_sec,
            "max_broadcast_sec": self._max_broadcast_sec,
        }

    async def _supervise(self) -> None:
        delay = 0.1
        while self._task is not None:
            handled_before = self._handled
            try:
                await self._task
                if self._stop_event.is_set():
                    return
                logger.error("event dispatcher exited unexpectedly; restarting")
            except asyncio.CancelledError:
                if self._stop_event.is_set():
                    return
                logger.error("event dispatcher was cancelled unexpectedly; restarting")
            except Exception:
                logger.exception("event dispatcher crashed; restarting")
                if self._stop_event.is_set():
                    return
            self._restarts += 1
            if self._handled > handled_before:
                delay = 0.1
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 5.0)
            self._task = asyncio.create_task(self._run(), name="event-dispatcher")

    async def _run(self) -> None:
        while True:
            event = self.runtime.read_event_nowait()
            if event is None:
                if self._stop_event.is_set():
                    return
                await asyncio.sleep(self.poll_interval_sec)
                continue
            started = time.monotonic()
            handling = asyncio.create_task(self._handle_event(event))
            cancelled = False
            try:
                try:
                    await asyncio.shield(handling)
                except asyncio.CancelledError:
                    cancelled = True
                    await handling
            except Exception:
                event_type = str(event.get("event_type", "unknown"))
                self._failed_by_type[event_type] = self._failed_by_type.get(event_type, 0) + 1
                logger.exception("event handling failed: type=%s id=%s", event_type, event.get("event_id"))
            finally:
                elapsed = time.monotonic() - started
                self._last_processing_sec = elapsed
                self._max_processing_sec = max(self._max_processing_sec, elapsed)
                self._handled += 1
            if cancelled:
                raise asyncio.CancelledError
            await asyncio.sleep(0)

    def _record_broadcast_drop(self, event_type: str) -> None:
        count = self._broadcast_drops_by_type.get(event_type, 0) + 1
        self._broadcast_drops_by_type[event_type] = count
        if count == 1 or count & (count - 1) == 0:
            logger.warning("broadcast queue dropped event: type=%s count=%d", event_type, count)

    def _enqueue_broadcast(self, payload: dict, full_payload: dict | None = None) -> None:
        event_type = str(payload.get("event_type", "unknown"))
        try:
            self._broadcast_queue.put_nowait((event_type, payload, full_payload))
        except asyncio.QueueFull:
            self._record_broadcast_drop(event_type)
            return
        size = self._broadcast_queue.qsize()
        self._broadcast_queue_high_water = max(self._broadcast_queue_high_water, size)
        pressured = size * 5 >= self._broadcast_queue.maxsize * 4
        if pressured and not self._broadcast_queue_pressure:
            logger.warning("broadcast queue at or above 80%%: size=%d max=%d", size, self._broadcast_queue.maxsize)
        self._broadcast_queue_pressure = pressured

    async def _broadcast_loop(self) -> None:
        while True:
            event_type, payload, full_payload = await self._broadcast_queue.get()
            started = time.monotonic()
            try:
                for channel, message in (("a", payload), ("queue", full_payload)):
                    if message is None:
                        continue
                    try:
                        if channel == "a":
                            await self._broadcast_channel_a(message)
                        else:
                            await self.ws_manager.broadcast_channel_queue(message)
                    except Exception:
                        self._broadcast_failures += 1
                        logger.exception("broadcast failed: type=%s channel=%s", event_type, channel)
            except asyncio.CancelledError:
                self._record_broadcast_drop(event_type)
                raise
            finally:
                elapsed = time.monotonic() - started
                self._last_broadcast_sec = elapsed
                self._max_broadcast_sec = max(self._max_broadcast_sec, elapsed)
                self._broadcast_queue.task_done()

    async def _debug_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                frame = self.runtime.read_debug_frame_nowait()
                if frame is None:
                    await asyncio.sleep(self.poll_interval_sec)
                    continue
                started = time.monotonic()
                await self.ws_manager.broadcast_channel_b(frame)
                elapsed = time.monotonic() - started
                self._last_broadcast_sec = elapsed
                self._max_broadcast_sec = max(self._max_broadcast_sec, elapsed)
            except Exception:
                self._broadcast_failures += 1
                logger.exception("debug frame broadcast failed")
                await asyncio.sleep(self.poll_interval_sec)

    async def _broadcast_channel_a(self, payload: dict) -> None:
        """Send a copy of payload to Channel A after stripping large image fields."""
        if isinstance(payload, dict):
            safe_payload = dict(payload)
            for field_name in (
                "faceimagebase64",
                "face_image_base64",
                "imagebase64",
                "image_base64",
            ):
                safe_payload.pop(field_name, None)

            for nested_key in ("data", "recognitiondata", "recognition_data"):
                nested_value = safe_payload.get(nested_key)
                if not isinstance(nested_value, dict):
                    continue
                cleaned_nested = dict(nested_value)
                for field_name in (
                    "faceimagebase64",
                    "face_image_base64",
                    "imagebase64",
                    "image_base64",
                ):
                    cleaned_nested.pop(field_name, None)
                safe_payload[nested_key] = cleaned_nested
        else:
            safe_payload = payload
        await self.ws_manager.broadcast_channel_a(safe_payload)

    @staticmethod
    def _make_light_payload(payload: dict) -> dict:
        if not isinstance(payload, dict):
            return payload

        safe_payload = dict(payload)
        for field_name in (
            "faceimagebase64",
            "face_image_base64",
            "imagebase64",
            "image_base64",
        ):
            safe_payload.pop(field_name, None)

        for nested_key in ("data", "recognitiondata", "recognition_data"):
            nested_value = safe_payload.get(nested_key)
            if not isinstance(nested_value, dict):
                continue
            cleaned_nested = dict(nested_value)
            for field_name in (
                "faceimagebase64",
                "face_image_base64",
                "imagebase64",
                "image_base64",
            ):
                cleaned_nested.pop(field_name, None)
            safe_payload[nested_key] = cleaned_nested

        return safe_payload

    async def _handle_event(self, event: dict) -> None:
        event_type = str(event.get("event_type", ""))

        if event_type in {"check_in", "recognition.success"}:
            normalized = self._normalize_success_event(event)
            persisted_payload = await asyncio.to_thread(self._persist_checkin_event, normalized)
            if persisted_payload is not None:
                if isinstance(persisted_payload, tuple):
                    light_payload, full_payload = persisted_payload
                    self._enqueue_broadcast(light_payload, full_payload)
                else:
                    self._enqueue_broadcast(persisted_payload)
            return

        if event_type == "recognition.pending":
            try:
                logger.info(
                    "received recognition.pending: event_id=%s timestamp=%s subject=%s similarity=%s",
                    event.get("event_id"),
                    event.get("timestamp"),
                    self._extract_subject_id_for_event(event),
                    self._extract_similarity_for_event(event),
                )
            except Exception:
                logger.exception("failed logging recognition.pending receipt")
            # Diagnose whether the event carries an image we can extract
            try:
                image_b64 = self._extract_image_base64_for_event(event)
                img_len = len(image_b64) if image_b64 else 0
                # Log minimal info for operators to trace missing images
                logger.info(
                    "recognition.pending: extracted_image_len=%d event_keys=%s data_keys=%s",
                    img_len,
                    list(event.keys()),
                    list(event.get("data", {}).keys()) if isinstance(event.get("data"), dict) else None,
                )
            except Exception:
                logger.exception("failed extracting image from recognition.pending")
            persisted_payload = await asyncio.to_thread(self._persist_pending_event, event)
            if persisted_payload is not None:
                light_payload, full_payload = persisted_payload
                self._enqueue_broadcast(light_payload, full_payload)
            return

        if event_type == "recognition_log":
            await asyncio.to_thread(self._persist_recognition_log_event, event)
            self._enqueue_broadcast(event)
            return

        if event_type == "debug_frame":
            await self.ws_manager.broadcast_channel_b(event)
            return

        if event_type in {"unknown_face", "recognition.unknown"}:
            persisted_payload = await asyncio.to_thread(self._persist_unknown_event, event)
            if persisted_payload is not None:
                light_payload, full_payload = persisted_payload
                self._enqueue_broadcast(light_payload, full_payload)
            return

        # Other runtime events go to channel A for ops visibility.
        self._enqueue_broadcast(event)

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
        if str(event.get("event_type", "")).startswith("recognition."):
            if isinstance(event.get("data"), dict):
                return dict(event.get("data") or {})
            if isinstance(event.get("recognitiondata"), dict):
                return dict(event.get("recognitiondata") or {})
            if isinstance(event.get("recognition_data"), dict):
                return dict(event.get("recognition_data") or {})
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
    def _extract_image_base64_for_event(cls, event: dict) -> str | None:
        data = cls._extract_business_data(event)
        recognition_data = {}
        if isinstance(event.get("recognitiondata"), dict):
            recognition_data = event.get("recognitiondata") or {}
        elif isinstance(event.get("recognition_data"), dict):
            recognition_data = event.get("recognition_data") or {}

        def first_non_empty_string(*values) -> str | None:
            for value in values:
                if value is None:
                    continue
                if isinstance(value, str):
                    candidate = value.strip()
                else:
                    candidate = str(value).strip()
                if candidate:
                    return candidate
            return None

        return first_non_empty_string(
            data.get("faceimagebase64"),
            data.get("face_image_base64"),
            data.get("imagebase64"),
            data.get("image_base64"),
            recognition_data.get("faceimagebase64"),
            recognition_data.get("face_image_base64"),
            recognition_data.get("imagebase64"),
            recognition_data.get("image_base64"),
            event.get("faceimagebase64"),
            event.get("face_image_base64"),
            event.get("imagebase64"),
            event.get("image_base64"),
        )

    def _persist_checkin_event(self, event: dict) -> dict | tuple[dict, dict]:
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
                if isinstance(orphaned_payload, tuple):
                    db.commit()
                    return orphaned_payload
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
                source=CheckInSource.RECOGNITION,
            )
            db.add(record)
            db.flush()

            log_reason = str(payload.get("method") or "").strip() or None
            existing_log = db.scalar(
                select(RecognitionLog).where(
                    RecognitionLog.timestamp == timestamp,
                    RecognitionLog.best_subject_id == subject_id,
                    RecognitionLog.status == RecognitionStatus.SUCCESS,
                )
            )
            if existing_log is None:
                self._create_recognition_log(
                    db,
                    status=RecognitionStatus.SUCCESS,
                    best_subject_id=subject_id,
                    similarity=payload.get("best_similarity", payload.get("similarity")),
                    second_subject_id=payload.get("second_subject_id"),
                    second_similarity=payload.get("second_similarity"),
                    timestamp=timestamp,
                    reason=log_reason,
                )

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
                "source": CheckInSource.RECOGNITION.value,
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
    ) -> tuple[dict, dict]:
        original_image = self._extract_image_base64_for_event(event)
        image_base64 = original_image or PLACEHOLDER_PNG_BASE64
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
                "face_image_base64": image_base64,
                "image_available": bool(original_image),
                "box": event.get("box"),
            },
        }
        if image_base64:
            payload["face_image_base64"] = image_base64

        full_payload = payload
        light_payload = self._make_light_payload(full_payload)
        return light_payload, full_payload

    def _persist_recognition_log_event(self, event: dict) -> None:
        status = self._safe_status(event.get("status"))
        reason = str(event.get("reason") or "").strip() or None
        with SessionLocal() as db:
            self._create_recognition_log(
                db,
                status=status,
                best_subject_id=str(event.get("best_subject_id", "unknown")),
                similarity=event.get("similarity"),
                second_subject_id=event.get("second_subject_id"),
                second_similarity=event.get("second_similarity"),
                timestamp=self._parse_timestamp(event.get("timestamp")),
                reason=reason,
            )
            db.commit()

    def _persist_pending_event(self, event: dict) -> tuple[dict, dict] | None:
        return self._persist_queue_event(event, decision="pending")

    def _persist_unknown_event(self, event: dict) -> tuple[dict, dict] | None:
        return self._persist_queue_event(event, decision="unknown")

    def _persist_queue_event(self, event: dict, decision: str) -> tuple[dict, dict] | None:
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
        try:
            img_len_preview = len(image_base64) if image_base64 else 0
        except Exception:
            img_len_preview = 0

        # Remember whether the original event actually carried an image
        original_image_present = bool(image_base64)

        # If there is no image, persist a small placeholder so the front-end shows a card.
        # Use a tiny 1x1 PNG placeholder (transparent) as base64.
        if not original_image_present:
            logger.info(
                "persist_queue_event: no image for decision=%s subject=%s timestamp=%s, using placeholder",
                decision,
                best_subject_id,
                timestamp.isoformat(),
            )
            image_base64 = PLACEHOLDER_PNG_BASE64
            try:
                img_len_preview = len(image_base64)
            except Exception:
                img_len_preview = 0

        with SessionLocal() as db:
            if self._should_suppress_unknown_after_successful_checkin(
                db,
                event=event,
                timestamp=timestamp,
                decision=decision,
            ):
                return None

            if decision == "pending" and best_subject_id:
                day_start = timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
                day_end = day_start + timedelta(days=1)
                existing_pending_case_id = db.scalar(
                    select(UnknownFaceCase.id).where(
                        UnknownFaceCase.status == UnknownCaseStatus.PENDING,
                        UnknownFaceCase.timestamp >= day_start,
                        UnknownFaceCase.timestamp < day_end,
                        UnknownFaceCase.note.contains(f"best_subject_id={best_subject_id}"),
                    )
                )
                if existing_pending_case_id is not None:
                    logger.info(
                        "persist_queue_event: suppressing duplicate pending case subject=%s date=%s existing_case_id=%s",
                        best_subject_id,
                        day_start.date().isoformat(),
                        str(existing_pending_case_id),
                    )
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
            # Flush to populate row.id before commit and log debug info about image
            db.flush()
            try:
                img_len = len(image_base64) if image_base64 else 0
            except Exception:
                img_len = 0
            logger.info(
                "persist_queue_event: created UnknownFaceCase id=%s decision=%s timestamp=%s queue_kind=%s image_len=%d",
                str(row.id),
                decision,
                timestamp.isoformat(),
                queue_kind,
                img_len,
            )
            try:
                db.commit()
                logger.info(
                    "persist_queue_event: commit success id=%s decision=%s timestamp=%s",
                    str(row.id),
                    decision,
                    timestamp.isoformat(),
                )
            except Exception:
                logger.exception(
                    "persist_queue_event: commit failed id=%s decision=%s timestamp=%s",
                    getattr(row, "id", None),
                    decision,
                    timestamp.isoformat(),
                )
                raise

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
                "face_image_base64": image_base64,
                # whether the original event provided a real image (not placeholder)
                "image_available": original_image_present,
                "box": data.get("box"),
            }
            if image_base64:
                payload["face_image_base64"] = image_base64

            full_payload = payload
            light_payload = self._make_light_payload(full_payload)
            return light_payload, full_payload

    def _should_suppress_unknown_after_successful_checkin(
        self,
        db,
        event: dict,
        timestamp: datetime,
        decision: str = "unknown",
    ) -> bool:
        if decision not in {"unknown", "pending"}:
            return False

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
        if existing is None:
            return False

        logger.info(
            "persist_queue_event: suppressing %s after successful check-in subject=%s date=%s member_id=%s",
            decision,
            best_subject_id,
            timestamp.date().isoformat(),
            str(member.id),
        )
        return True

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
        reason: str | None = None,
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
            reason=reason,
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
        try:
            return RecognitionStatus(value)
        except (ValueError, KeyError):
            logger.warning("unknown recognition status value: %r, falling back to UNKNOWN", value)
            return RecognitionStatus.UNKNOWN
