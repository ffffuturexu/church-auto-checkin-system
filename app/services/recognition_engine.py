from __future__ import annotations

import base64
import logging
import queue
import threading
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

import cv2
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

from app.api import CompreFaceClient
from app.config import settings
from app.core.time_utils import now_local

logger = logging.getLogger(__name__)


@dataclass
class RecognitionHyperParams:
    absolute_threshold: float = 0.80
    vote_threshold: float = 0.70
    margin: float = 0.18
    dedupe_seconds: int = 60
    frame_skip: int = 2
    vote_window_sec: float = 1.5
    vote_min_samples: int = 5
    vote_ratio: float = 0.65
    unknown_min_similarity: float = 0.65
    unknown_min_face_size: int = 64
    pending_min_similarity: float = 0.70
    pending_min_frames: int = 4
    stranger_max_similarity: float = 0.56
    stranger_window_sec: float = 1.5
    stranger_min_frames: int = 4
    max_finalize_sec: float = 6.0


class RecognitionEngine:
    """Background recognition engine with voting and dedupe."""

    def __init__(
        self,
        api_client: CompreFaceClient | None = None,
        params: RecognitionHyperParams | None = None,
        input_queue_size: int = 8,
        event_queue_size: int = 256,
        recognition_workers: int = 3,
        max_inflight_requests: int | None = None,
    ) -> None:
        self.api_client = api_client or CompreFaceClient()
        vote_threshold = float(getattr(settings, "VOTE_THRESHOLD", getattr(settings, "THRESHOLD", 0.70)))
        absolute_threshold = float(getattr(settings, "ABSOLUTE_THRESHOLD", vote_threshold + 0.10))
        unknown_min_similarity = float(getattr(settings, "UNKNOWN_MIN_SIMILARITY", 0.65))
        self.params = params or RecognitionHyperParams(
            absolute_threshold=absolute_threshold,
            vote_threshold=vote_threshold,
            margin=settings.MARGIN,
            dedupe_seconds=settings.DEDUPE_SECONDS,
            frame_skip=settings.FRAME_SKIP,
            vote_window_sec=settings.VOTE_WINDOW_SEC,
            vote_min_samples=settings.VOTE_MIN_SAMPLES,
            vote_ratio=settings.VOTE_RATIO,
            unknown_min_similarity=unknown_min_similarity,
            unknown_min_face_size=settings.UNKNOWN_MIN_FACE_SIZE,
            pending_min_similarity=float(getattr(settings, "PENDING_MIN_SIMILARITY", unknown_min_similarity)),
            pending_min_frames=int(getattr(settings, "PENDING_MIN_FRAMES", max(2, settings.VOTE_MIN_SAMPLES))),
            stranger_max_similarity=float(
                getattr(
                    settings,
                    "STRANGER_MAX_SIMILARITY",
                    max(0.0, min(vote_threshold - 0.02, unknown_min_similarity - 0.02)),
                )
            ),
            stranger_window_sec=float(getattr(settings, "STRANGER_WINDOW_SEC", settings.VOTE_WINDOW_SEC)),
            stranger_min_frames=int(getattr(settings, "STRANGER_MIN_FRAMES", max(2, settings.VOTE_MIN_SAMPLES))),
            max_finalize_sec=float(getattr(settings, "MAX_FINALIZE_SEC", 6.0)),
        )
        self.params = self._normalize_hyperparams(self.params)

        self.frame_queue: queue.Queue[Any] = queue.Queue(maxsize=max(1, input_queue_size))
        self.event_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max(1, event_queue_size))

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None

        self._frame_count = 0
        self._last_seen: dict[str, datetime] = {}
        self._unknown_last_seen: dict[str, datetime] = {}
        self._unknown_cooldown_sec = 2.0
        self._vote_buffers: dict[str, deque[tuple[datetime, float]]] = {}
        self._vote_buffer_last_seen: dict[str, datetime] = {}
        self._pending_candidates: dict[str, deque[dict[str, Any]]] = {}
        self._pending_last_seen: dict[str, datetime] = {}
        self._pending_snapshots: dict[str, tuple[Any, dict[str, int] | None, str | None, float | None]] = {}
        self._stranger_candidates: dict[str, deque[tuple[datetime, float]]] = {}
        self._stranger_last_seen: dict[str, datetime] = {}
        self._stranger_snapshots: dict[str, tuple[Any, dict[str, int] | None, float]] = {}
        self._pending_requests: deque[tuple[Any, Future]] = deque()
        self._recognition_workers = max(1, int(recognition_workers))
        self._max_inflight_requests = max(
            self._recognition_workers,
            int(max_inflight_requests if max_inflight_requests is not None else self._recognition_workers * 2),
        )
        self._last_event_at: datetime | None = None
        self._last_debug_frame_at: datetime | None = None
        self._last_error_at: datetime | None = None
        self._last_result_at: datetime | None = None
        self._debug_enabled = True
        self._debug_overlay_enabled = True
        self._subject_name_resolver: Callable[[str], str | None] | None = None
        self._subject_name_cache: dict[str, tuple[str | None, datetime]] = {}
        self._subject_name_cache_ttl_sec = 120.0

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._recognition_workers,
                    thread_name_prefix="recognition-http",
                )
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="recognition-engine", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        with self._lock:
            while self._pending_requests:
                _, future = self._pending_requests.popleft()
                if not future.done():
                    future.cancel()
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())

    def submit_frame(self, frame: Any) -> None:
        if self.frame_queue.full():
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self.frame_queue.put_nowait(frame)
        except queue.Full:
            pass

    def read_event_nowait(self) -> dict[str, Any] | None:
        try:
            return self.event_queue.get_nowait()
        except queue.Empty:
            return None

    def update_hyperparams(self, **kwargs: Any) -> RecognitionHyperParams:
        with self._lock:
            for field_name in self.params.__dataclass_fields__.keys():
                if field_name in kwargs and kwargs[field_name] is not None:
                    setattr(self.params, field_name, kwargs[field_name])

            self.params = self._normalize_hyperparams(self.params)
            updated = RecognitionHyperParams(**asdict(self.params))
        self._emit_event("hyperparams_updated", updated=asdict(updated))
        return updated

    def get_hyperparams(self) -> dict[str, Any]:
        with self._lock:
            return asdict(self.params)

    def get_runtime_stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "pending_requests": len(self._pending_requests),
                "last_event_at": self._format_timestamp(self._last_event_at),
                "last_debug_frame_at": self._format_timestamp(self._last_debug_frame_at),
                "last_error_at": self._format_timestamp(self._last_error_at),
                "last_result_at": self._format_timestamp(self._last_result_at),
                "debug_video_enabled": self._debug_enabled,
                "debug_overlay_enabled": self._debug_overlay_enabled,
            }

    def set_debug_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._debug_enabled = bool(enabled)
        self._emit_event("debug_video_toggled", enabled=self._debug_enabled)

    def is_debug_enabled(self) -> bool:
        with self._lock:
            return bool(self._debug_enabled)

    def set_debug_overlay_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._debug_overlay_enabled = bool(enabled)
        self._emit_event("debug_overlay_toggled", enabled=self._debug_overlay_enabled)

    def is_debug_overlay_enabled(self) -> bool:
        with self._lock:
            return bool(self._debug_overlay_enabled)

    def set_subject_name_resolver(self, resolver: Callable[[str], str | None] | None) -> None:
        with self._lock:
            self._subject_name_resolver = resolver
            self._subject_name_cache.clear()

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._drain_completed_requests()
                try:
                    frame = self.frame_queue.get(timeout=0.05)
                except queue.Empty:
                    continue

                self._frame_count += 1
                skip = max(1, int(self.params.frame_skip))
                if self._frame_count % skip != 0:
                    continue

                self._submit_recognition_request(frame)
        finally:
            self._drain_completed_requests(flush_all=True)

    def _submit_recognition_request(self, frame: Any) -> None:
        if len(self._pending_requests) >= self._max_inflight_requests:
            return

        try:
            ok, encoded = cv2.imencode(".jpg", frame)
            if not ok:
                return
            if self._executor is None:
                return
            future = self._executor.submit(self.api_client.recognize_image, encoded.tobytes())
            self._pending_requests.append((frame, future))
        except Exception as exc:  # pragma: no cover - defensive path
            self._emit_event("recognition_error", error=str(exc))

    def _drain_completed_requests(self, flush_all: bool = False) -> None:
        if not self._pending_requests:
            return

        remaining: deque[tuple[Any, Future]] = deque()
        while self._pending_requests:
            frame, future = self._pending_requests.popleft()

            if not future.done() and not flush_all:
                remaining.append((frame, future))
                continue

            if not future.done() and flush_all:
                future.cancel()
                continue

            try:
                result = future.result()
            except requests.exceptions.HTTPError as exc:
                self._handle_recognition_http_error(frame, exc)
                continue
            except requests.exceptions.RequestException as exc:
                self._emit_event("recognition_error", error=str(exc))
                continue
            except Exception as exc:  # pragma: no cover - defensive path
                self._emit_event("recognition_error", error=str(exc))
                continue

            self._handle_recognition_result(frame, result)

        self._pending_requests = remaining

    def _handle_recognition_http_error(self, frame: Any, exc: requests.exceptions.HTTPError) -> None:
        if exc.response is not None and exc.response.status_code == 400:
            try:
                payload = exc.response.json()
            except ValueError:
                payload = {}
            message = str(payload.get("message", ""))
            if payload.get("code") == 28 or "face is not found" in message.lower():
                self._last_result_at = now_local()
                self._emit_debug_frame(frame, box=None, best_subject_id="No Face", similarity=0.0, status="scanning")
                return
        self._last_error_at = now_local()
        self._emit_event("recognition_error", error=str(exc))

    def _handle_recognition_result(self, frame: Any, result: dict[str, Any]) -> None:
        detections = result.get("result", [])
        now = now_local()
        self._last_result_at = now
        self._prune_subject_state(now)

        if not detections:
            self._emit_debug_frame(frame, box=None, best_subject_id="No Face", similarity=0.0, status="scanning")
            self._finalize_pending_candidates(now)
            self._finalize_stranger_candidates(now)
            return

        debug_annotations: list[dict[str, Any]] = []

        for item in detections:
            box = self._extract_box(item)
            subjects = item.get("subjects", [])
            best, second = self._pick_best_two(subjects)

            best_subject = str(best.get("subject", "")).strip() if best else ""
            best_similarity = float(best.get("similarity", 0.0) or 0.0) if best else 0.0
            second_subject = second.get("subject") if second is not None else None
            second_similarity = float(second.get("similarity", 0.0) or 0.0) if second is not None else 0.0

            vote_threshold = self._get_vote_threshold()
            absolute_threshold = self._get_absolute_threshold(vote_threshold)
            margin = float(self.params.margin)

            if best_subject and best_similarity >= absolute_threshold and (best_similarity - second_similarity) >= margin:
                self._clear_subject_vote_state(best_subject)
                self._clear_pending_state(best_subject)
                self._commit_checkin(
                    best_subject,
                    best_similarity,
                    frame=frame,
                    box=box,
                    second_subject_id=second_subject,
                    second_similarity=second_similarity,
                    method="auto_face_absolute",
                )
                debug_annotations.append(
                    {
                        "box": box,
                        "best_subject_id": best_subject,
                        "similarity": best_similarity,
                        "status": "candidate",
                    }
                )
                continue

            if best_subject and best_similarity >= float(self.params.pending_min_similarity):
                self._track_pending_candidate(
                    subject=best_subject,
                    similarity=best_similarity,
                    second_subject_id=str(second_subject).strip() if second_subject else None,
                    second_similarity=second_similarity,
                    frame=frame,
                    box=box,
                )
                committed = self._add_vote_and_maybe_checkin(
                    best_subject,
                    best_similarity,
                    frame=frame,
                    box=box,
                    second_subject_id=str(second_subject).strip() if second_subject else None,
                    second_similarity=second_similarity,
                )
                debug_annotations.append(
                    {
                        "box": box,
                        "best_subject_id": best_subject,
                        "similarity": best_similarity,
                        "status": "candidate" if committed else "tracking",
                    }
                )
                continue

            # Stranger tracking: no known subject, or known subject but similarity is low enough
            # to be closer to stranger/unregistered profile.
            if (not best_subject) or (best_similarity <= float(self.params.stranger_max_similarity)):
                self._track_stranger_observation(frame=frame, box=box, similarity=best_similarity)
                debug_annotations.append(
                    {
                        "box": box,
                        "best_subject_id": "Unknown",
                        "similarity": best_similarity,
                        "status": "scanning",
                    }
                )
                continue

            logger.info(
                "recognition no_decision: subject=%s similarity=%.3f",
                best_subject or "<none>",
                best_similarity,
            )
            debug_annotations.append(
                {
                    "box": box,
                    "best_subject_id": best_subject or "Unknown",
                    "similarity": best_similarity,
                    "status": "no_decision",
                }
            )

        self._finalize_pending_candidates(now)
        self._finalize_stranger_candidates(now)

        if debug_annotations:
            self._emit_debug_frame_batch(frame, debug_annotations)

    def _track_pending_candidate(
        self,
        subject: str,
        similarity: float,
        second_subject_id: str | None,
        second_similarity: float,
        frame: Any,
        box: dict[str, int] | None,
    ) -> None:
        now = now_local()
        window = max(0.2, float(self.params.vote_window_sec))
        cutoff = now - timedelta(seconds=window)

        subject_buffer = self._pending_candidates.get(subject)
        if subject_buffer is None:
            subject_buffer = deque()
            self._pending_candidates[subject] = subject_buffer

        subject_buffer.append(
            {
                "timestamp": now,
                "similarity": float(similarity),
                "second_subject_id": second_subject_id,
                "second_similarity": float(second_similarity),
            }
        )
        while subject_buffer and subject_buffer[0]["timestamp"] < cutoff:
            subject_buffer.popleft()

        self._pending_last_seen[subject] = now
        self._pending_snapshots[subject] = (frame, box, second_subject_id, second_similarity)

    def _finalize_pending_candidates(self, now: datetime) -> None:
        if not self._pending_candidates:
            return

        window_sec = max(0.2, float(self.params.vote_window_sec))
        max_finalize_sec = max(window_sec, float(getattr(self.params, "max_finalize_sec", window_sec)))
        min_frames = max(1, int(self.params.pending_min_frames))
        vote_threshold = self._get_vote_threshold()
        vote_ratio = float(self.params.vote_ratio)
        margin = float(self.params.margin)

        for subject_id, buffer in list(self._pending_candidates.items()):
            if not buffer:
                self._clear_pending_state(subject_id)
                continue

            first_ts = buffer[0]["timestamp"]
            last_ts = buffer[-1]["timestamp"]
            elapsed = (now - first_ts).total_seconds()
            idle = (now - last_ts).total_seconds()
            # Finalize primarily on idle timeout; max_finalize_sec is a safety valve
            # to avoid candidates lingering forever in noisy streams.
            if idle < window_sec and elapsed < max_finalize_sec:
                continue

            samples = list(buffer)
            self._clear_pending_state(subject_id)

            if len(samples) < min_frames:
                logger.info(
                    "recognition discarded: subject=%s reason=insufficient_frames samples=%d",
                    subject_id,
                    len(samples),
                )
                continue

            max_similarity = max(float(item["similarity"]) for item in samples)
            if max_similarity < float(self.params.pending_min_similarity):
                logger.info(
                    "recognition discarded: subject=%s reason=weak_candidate similarity=%.3f",
                    subject_id,
                    max_similarity,
                )
                continue

            qualified_count = sum(1 for item in samples if float(item["similarity"]) >= vote_threshold)
            qualified_ratio = qualified_count / max(1, len(samples))
            max_margin = max(
                float(item["similarity"]) - float(item.get("second_similarity") or 0.0)
                for item in samples
            )

            reason = "vote_timeout"
            if max_margin < margin:
                reason = "ambiguous_margin"
            elif qualified_ratio < vote_ratio:
                reason = "weak_consensus"

            frame, box, second_subject_id, second_similarity = self._pending_snapshots.get(
                subject_id,
                (None, None, None, None),
            )

            self._emit_log(
                status="pending",
                best_subject_id=subject_id,
                similarity=max_similarity,
                second_subject_id=second_subject_id,
                second_similarity=second_similarity,
            )
            self._emit_business_event(
                event_type="recognition.pending",
                decision="pending",
                method="pending_review",
                subject_id=subject_id,
                similarity=max_similarity,
                best_similarity=max_similarity,
                second_subject_id=second_subject_id,
                second_similarity=second_similarity,
                reason=reason,
                face_image_base64=self._encode_unknown_face(frame, box) if frame is not None else None,
                box=box,
            )

    def _track_stranger_observation(self, frame: Any, box: dict[str, int] | None, similarity: float) -> None:
        if not self._passes_unknown_box_filter(box):
            return

        now = now_local()
        window_sec = max(0.2, float(self.params.stranger_window_sec))
        cutoff = now - timedelta(seconds=window_sec)
        key = self._build_unknown_key(box) or "global"

        candidate_buffer = self._stranger_candidates.get(key)
        if candidate_buffer is None:
            candidate_buffer = deque()
            self._stranger_candidates[key] = candidate_buffer

        candidate_buffer.append((now, float(similarity)))
        while candidate_buffer and candidate_buffer[0][0] < cutoff:
            candidate_buffer.popleft()

        self._stranger_last_seen[key] = now
        snapshot = self._stranger_snapshots.get(key)
        if snapshot is None or float(similarity) >= float(snapshot[2]):
            self._stranger_snapshots[key] = (frame, box, float(similarity))

    def _finalize_stranger_candidates(self, now: datetime) -> None:
        if not self._stranger_candidates:
            return

        min_frames = max(1, int(self.params.stranger_min_frames))
        max_similarity_threshold = float(self.params.stranger_max_similarity)
        window_sec = max(0.2, float(self.params.stranger_window_sec))
        max_finalize_sec = max(window_sec, float(getattr(self.params, "max_finalize_sec", window_sec)))

        for key, buffer in list(self._stranger_candidates.items()):
            if not buffer:
                self._clear_stranger_state(key)
                continue

            first_ts = buffer[0][0]
            last_ts = buffer[-1][0]
            elapsed = (now - first_ts).total_seconds()
            idle = (now - last_ts).total_seconds()
            if idle < window_sec and elapsed < max_finalize_sec:
                continue

            samples = list(buffer)
            self._clear_stranger_state(key)

            if len(samples) < min_frames:
                logger.info("recognition discarded: stranger reason=insufficient_frames samples=%d", len(samples))
                continue

            max_similarity = max(item[1] for item in samples)
            if max_similarity > max_similarity_threshold:
                logger.info(
                    "recognition no_decision: stranger reason=similarity_too_high similarity=%.3f",
                    max_similarity,
                )
                continue

            frame, box, _ = self._stranger_snapshots.get(key, (None, None, 0.0))
            if frame is None:
                continue

            cooldown_key = self._build_unknown_key(box)
            now_for_cooldown = now_local()
            if cooldown_key is not None:
                last_seen = self._unknown_last_seen.get(cooldown_key)
                if last_seen is not None:
                    elapsed_sec = (now_for_cooldown - last_seen).total_seconds()
                    if elapsed_sec < self._unknown_cooldown_sec:
                        continue

            image_b64 = self._encode_unknown_face(frame, box)
            if image_b64 is None:
                continue

            if cooldown_key is not None:
                self._unknown_last_seen[cooldown_key] = now_for_cooldown
                stale_before = now_for_cooldown - timedelta(seconds=self._unknown_cooldown_sec * 4)
                stale_keys = [k for k, ts in self._unknown_last_seen.items() if ts < stale_before]
                for stale_key in stale_keys:
                    self._unknown_last_seen.pop(stale_key, None)

            self._emit_log(
                status="unknown",
                best_subject_id="unknown",
                similarity=max_similarity,
                second_subject_id=None,
                second_similarity=None,
            )
            self._emit_business_event(
                event_type="recognition.unknown",
                decision="unknown",
                method="stranger_detected",
                subject_id=None,
                similarity=max_similarity,
                best_similarity=max_similarity,
                second_subject_id=None,
                second_similarity=None,
                reason="stranger_low_similarity",
                face_image_base64=image_b64,
                box=box,
            )

    def _prune_subject_state(self, now: datetime) -> None:
        self._prune_vote_buffers(now=now, cutoff=now - timedelta(seconds=max(0.1, float(self.params.vote_window_sec))))
        stale_pending_cutoff = now - timedelta(seconds=max(5.0, float(self.params.vote_window_sec) * 3.0))
        for subject_id, last_seen in list(self._pending_last_seen.items()):
            if last_seen < stale_pending_cutoff:
                self._clear_pending_state(subject_id)

        stale_stranger_cutoff = now - timedelta(seconds=max(5.0, float(self.params.stranger_window_sec) * 3.0))
        for key, last_seen in list(self._stranger_last_seen.items()):
            if last_seen < stale_stranger_cutoff:
                self._clear_stranger_state(key)

    def _clear_pending_state(self, subject_id: str) -> None:
        self._pending_candidates.pop(subject_id, None)
        self._pending_last_seen.pop(subject_id, None)
        self._pending_snapshots.pop(subject_id, None)

    def _clear_stranger_state(self, key: str) -> None:
        self._stranger_candidates.pop(key, None)
        self._stranger_last_seen.pop(key, None)
        self._stranger_snapshots.pop(key, None)

    def _add_vote_and_maybe_checkin(
        self,
        subject: str,
        similarity: float,
        frame: Any,
        box: dict[str, int] | None,
        second_subject_id: str | None = None,
        second_similarity: float | None = None,
    ) -> bool:
        now = now_local()
        window = max(0.1, float(self.params.vote_window_sec))
        cutoff = now - timedelta(seconds=window)
        self._prune_vote_buffers(now=now, cutoff=cutoff)

        subject_buffer = self._vote_buffers.get(subject)
        if subject_buffer is None:
            subject_buffer = deque()
            self._vote_buffers[subject] = subject_buffer

        subject_buffer.append((now, similarity))
        self._vote_buffer_last_seen[subject] = now

        while subject_buffer and subject_buffer[0][0] < cutoff:
            subject_buffer.popleft()

        if not subject_buffer:
            self._vote_buffers.pop(subject, None)
            self._vote_buffer_last_seen.pop(subject, None)
            return False

        min_samples = max(1, int(self.params.vote_min_samples))
        if len(subject_buffer) < min_samples:
            return False

        vote_threshold = self._get_vote_threshold()
        qualified_sims = [item[1] for item in subject_buffer if float(item[1]) >= vote_threshold]
        qualified_ratio = len(qualified_sims) / max(1, len(subject_buffer))
        if qualified_ratio < float(self.params.vote_ratio):
            return False

        if not qualified_sims:
            return False

        final_similarity = max(qualified_sims)
        self._vote_buffers.pop(subject, None)
        self._vote_buffer_last_seen.pop(subject, None)
        self._clear_pending_state(subject)
        self._commit_checkin(
            subject,
            final_similarity,
            frame=frame,
            box=box,
            second_subject_id=second_subject_id,
            second_similarity=second_similarity,
            method="auto_face_vote",
        )
        return True

    def _prune_vote_buffers(self, now: datetime, cutoff: datetime) -> None:
        stale_subject_cutoff = now - timedelta(seconds=max(10.0, float(self.params.vote_window_sec) * 4.0))

        for subject_id, subject_buffer in list(self._vote_buffers.items()):
            while subject_buffer and subject_buffer[0][0] < cutoff:
                subject_buffer.popleft()
            if not subject_buffer:
                self._vote_buffers.pop(subject_id, None)
                self._vote_buffer_last_seen.pop(subject_id, None)

        for subject_id, last_seen in list(self._vote_buffer_last_seen.items()):
            if last_seen < stale_subject_cutoff:
                self._vote_buffer_last_seen.pop(subject_id, None)
                self._vote_buffers.pop(subject_id, None)

    def _commit_checkin(
        self,
        subject_id: str,
        similarity: float,
        frame: Any | None,
        box: dict[str, int] | None,
        second_subject_id: str | None = None,
        second_similarity: float | None = None,
        method: str = "auto_face",
    ) -> None:
        now = now_local()
        dedupe_window = timedelta(seconds=max(0, int(self.params.dedupe_seconds)))
        last = self._last_seen.get(subject_id)

        if last is not None and now - last <= dedupe_window:
            self._emit_event(
                "deduped",
                subject_id=subject_id,
                similarity=similarity,
                timestamp=now.isoformat(timespec="seconds"),
            )
            return

        self._last_seen[subject_id] = now

        timestamp = now.isoformat(timespec="seconds")
        face_image_base64 = None
        if frame is not None:
            face_image_base64 = self._encode_unknown_face(frame, box)

        self._emit_log(
            status="success",
            best_subject_id=subject_id,
            similarity=similarity,
            second_subject_id=second_subject_id,
            second_similarity=second_similarity,
        )
        self._emit_business_event(
            event_type="recognition.success",
            decision="success",
            method=method,
            subject_id=subject_id,
            similarity=similarity,
            best_similarity=similarity,
            second_subject_id=second_subject_id,
            second_similarity=second_similarity,
            reason=None,
            face_image_base64=face_image_base64,
            box=box,
            timestamp=timestamp,
        )

    def _emit_unknown(
        self,
        frame: Any,
        box: dict[str, int] | None,
        reason: str,
        best_subject_id: str | None = None,
        similarity: float | None = None,
        second_subject_id: str | None = None,
        second_similarity: float | None = None,
    ) -> None:
        # Legacy method kept for compatibility with old tests/paths.
        image_b64 = self._encode_unknown_face(frame, box)
        self._emit_business_event(
            event_type="recognition.unknown",
            decision="unknown",
            method="stranger_detected",
            subject_id=None,
            similarity=similarity,
            best_similarity=similarity,
            second_subject_id=second_subject_id,
            second_similarity=second_similarity,
            reason=reason,
            face_image_base64=image_b64,
            box=box,
        )

    def _passes_unknown_box_filter(self, box: dict[str, int] | None) -> bool:
        if box is None:
            return True

        width = max(0, int(box.get("width", 0)))
        height = max(0, int(box.get("height", 0)))
        if width <= 0 or height <= 0:
            return False

        min_side = max(8, int(self.params.unknown_min_face_size))
        return min(width, height) >= min_side

    def _emit_log(
        self,
        status: str,
        best_subject_id: str,
        similarity: float,
        second_subject_id: str | None,
        second_similarity: float | None,
    ) -> None:
        self._emit_event(
            "recognition_log",
            timestamp=now_local().isoformat(timespec="seconds"),
            status=status,
            best_subject_id=best_subject_id,
            similarity=similarity,
            second_subject_id=second_subject_id,
            second_similarity=second_similarity,
        )

    def _emit_business_event(
        self,
        event_type: str,
        decision: str,
        method: str,
        subject_id: str | None,
        similarity: float | None,
        best_similarity: float | None,
        second_subject_id: str | None,
        second_similarity: float | None,
        reason: str | None,
        face_image_base64: str | None,
        box: dict[str, int] | None,
        timestamp: str | None = None,
    ) -> None:
        event_timestamp = timestamp or now_local().isoformat(timespec="seconds")
        payload: dict[str, Any] = {
            "event_id": str(uuid.uuid4()),
            "timestamp": event_timestamp,
            "camera_id": None,
            "data": {
                "decision": decision,
                "method": method,
                "subject_id": subject_id,
                "subject_name": self._resolve_subject_label(subject_id) if subject_id else None,
                "similarity": float(similarity) if similarity is not None else None,
                "best_similarity": float(best_similarity) if best_similarity is not None else None,
                "second_subject_id": second_subject_id,
                "second_similarity": float(second_similarity) if second_similarity is not None else None,
                "reason": reason,
                "face_image_base64": face_image_base64,
                "box": box,
            },
        }
        self._emit_event(event_type, **payload)

    def _emit_debug_frame(
        self,
        frame: Any,
        box: dict[str, int] | None,
        best_subject_id: str,
        similarity: float,
        status: str,
    ) -> None:
        self._emit_debug_frame_batch(
            frame,
            [
                {
                    "box": box,
                    "best_subject_id": best_subject_id,
                    "similarity": similarity,
                    "status": status,
                }
            ],
        )

    def _emit_debug_frame_batch(self, frame: Any, annotations: list[dict[str, Any]]) -> None:
        if not self._debug_enabled:
            return
        self._last_debug_frame_at = now_local()
        debug_frame = frame.copy()
        if self.is_debug_overlay_enabled():
            for annotation in annotations:
                box = annotation.get("box")
                if box is None:
                    continue
                x_min = int(box.get("x_min", 0))
                y_min = int(box.get("y_min", 0))
                width = int(box.get("width", 0))
                height = int(box.get("height", 0))
                x_max = x_min + max(0, width)
                y_max = y_min + max(0, height)
                similarity = float(annotation.get("similarity", 0.0) or 0.0)
                color = self._get_similarity_color(similarity)
                subject_id = str(annotation.get("best_subject_id", ""))
                label = self._resolve_subject_label(subject_id)
                cv2.rectangle(debug_frame, (x_min, y_min), (x_max, y_max), color, 2)
                debug_frame = self._draw_debug_label(debug_frame, x_min, y_min, label, similarity, color)

        resized = cv2.resize(debug_frame, (640, 360), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(
            ".jpg",
            resized,
            [int(cv2.IMWRITE_JPEG_QUALITY), 50],
        )
        if not ok:
            return

        status = "scanning"
        if annotations:
            status = str(annotations[-1].get("status", "scanning"))

        self._emit_event(
            "debug_frame",
            timestamp=now_local().isoformat(timespec="seconds"),
            status=status,
            image_base64=base64.b64encode(encoded.tobytes()).decode("ascii"),
        )

    @staticmethod
    def _draw_debug_label(
        frame: Any,
        x_min: int,
        y_min: int,
        label: str,
        similarity: float,
        color: tuple[int, int, int],
    ) -> Any:
        text = f"{label} {similarity:.3f}"
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb_frame)
        draw = ImageDraw.Draw(image)
        font = RecognitionEngine._load_debug_font(max(18, min(image.size) // 18))

        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=2)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        padding_x = 8
        padding_y = 5
        background_width = text_width + padding_x * 2
        background_height = text_height + padding_y * 2

        background_x = max(0, min(x_min, image.size[0] - background_width))
        background_y = max(0, y_min - background_height - 8)
        if background_y + background_height > image.size[1]:
            background_y = max(0, min(image.size[1] - background_height, y_min + 8))

        background_box = [
            background_x,
            background_y,
            min(image.size[0], background_x + background_width),
            min(image.size[1], background_y + background_height),
        ]
        draw.rounded_rectangle(background_box, radius=8, fill=(15, 24, 18))

        text_x = background_x + padding_x
        text_y = background_y + padding_y - bbox[1]
        draw.text(
            (text_x, text_y),
            text,
            font=font,
            fill=(255, 255, 255),
            stroke_width=2,
            stroke_fill=color,
        )

        return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

    @staticmethod
    def _load_debug_font(size: int) -> ImageFont.ImageFont:
        candidates = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        ]
        for font_path in candidates:
            try:
                return ImageFont.truetype(font_path, size=size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _get_similarity_color(self, similarity: float) -> tuple[int, int, int]:
        threshold = self._get_vote_threshold()
        green_floor = min(0.99, threshold + 0.10)
        if similarity >= green_floor:
            return (0, 200, 0)
        if similarity >= threshold:
            return (0, 200, 200)
        return (0, 0, 200)

    def _get_vote_threshold(self) -> float:
        return float(getattr(self.params, "vote_threshold", 0.70))

    @staticmethod
    def _normalize_hyperparams(params: RecognitionHyperParams) -> RecognitionHyperParams:
        normalized = RecognitionHyperParams(**asdict(params))
        eps = 1e-3

        normalized.absolute_threshold = max(0.0, min(1.0, float(normalized.absolute_threshold)))
        normalized.vote_threshold = max(0.0, min(1.0, float(normalized.vote_threshold)))
        normalized.pending_min_similarity = max(0.0, min(1.0, float(normalized.pending_min_similarity)))
        normalized.stranger_max_similarity = max(0.0, min(1.0, float(normalized.stranger_max_similarity)))

        # Enforce ordering: stranger < pending <= vote <= absolute
        if normalized.pending_min_similarity > normalized.vote_threshold:
            normalized.pending_min_similarity = normalized.vote_threshold
        if normalized.vote_threshold > normalized.absolute_threshold:
            normalized.vote_threshold = normalized.absolute_threshold
        if normalized.pending_min_similarity > normalized.vote_threshold:
            normalized.pending_min_similarity = normalized.vote_threshold

        max_stranger = max(0.0, normalized.pending_min_similarity - eps)
        if normalized.stranger_max_similarity >= normalized.pending_min_similarity:
            normalized.stranger_max_similarity = max_stranger

        normalized.margin = max(0.0, min(1.0, float(normalized.margin)))
        normalized.vote_ratio = max(0.0, min(1.0, float(normalized.vote_ratio)))
        normalized.vote_window_sec = max(0.1, float(normalized.vote_window_sec))
        normalized.stranger_window_sec = max(0.1, float(normalized.stranger_window_sec))
        normalized.max_finalize_sec = max(0.2, float(normalized.max_finalize_sec))
        normalized.vote_min_samples = max(1, int(normalized.vote_min_samples))
        normalized.pending_min_frames = max(1, int(normalized.pending_min_frames))
        normalized.stranger_min_frames = max(1, int(normalized.stranger_min_frames))
        normalized.frame_skip = max(1, int(normalized.frame_skip))
        normalized.dedupe_seconds = max(0, int(normalized.dedupe_seconds))
        normalized.unknown_min_face_size = max(8, int(normalized.unknown_min_face_size))

        # unknown_min_similarity is kept for backward compatibility only.
        normalized.unknown_min_similarity = max(0.0, min(1.0, float(normalized.unknown_min_similarity)))
        return normalized

    def _get_absolute_threshold(self, vote_threshold: float | None = None) -> float:
        fallback_vote_threshold = self._get_vote_threshold() if vote_threshold is None else float(vote_threshold)
        return float(getattr(self.params, "absolute_threshold", fallback_vote_threshold))

    def _clear_subject_vote_state(self, subject_id: str) -> None:
        self._vote_buffers.pop(subject_id, None)
        self._vote_buffer_last_seen.pop(subject_id, None)

    def _resolve_subject_label(self, subject_id: str) -> str:
        if subject_id in {"", "Unknown", "No Face"}:
            return subject_id

        with self._lock:
            resolver = self._subject_name_resolver
            cached = self._subject_name_cache.get(subject_id)

        now = now_local()
        if cached is not None:
            cached_name, cached_at = cached
            if (now - cached_at).total_seconds() <= self._subject_name_cache_ttl_sec:
                return cached_name or subject_id

        if resolver is None:
            return subject_id

        name = None
        try:
            name = resolver(subject_id)
        except Exception:
            name = None

        with self._lock:
            self._subject_name_cache[subject_id] = (name, now)

        return name or subject_id

    def _emit_event(self, event_type: str, **payload: Any) -> None:
        self._last_event_at = now_local()
        event = {
            "event_type": event_type,
            **payload,
        }
        if self.event_queue.full():
            try:
                self.event_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self.event_queue.put_nowait(event)
        except queue.Full:
            pass

    @staticmethod
    def _format_timestamp(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.isoformat(timespec="seconds")

    @staticmethod
    def _pick_best_two(subjects: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not subjects:
            return None, None

        best = subjects[0]
        best_subject = best.get("subject")
        second = None
        for candidate in subjects[1:]:
            if candidate.get("subject") != best_subject:
                second = candidate
                break
        return best, second

    @staticmethod
    def _extract_box(item: dict[str, Any]) -> dict[str, int] | None:
        source = item.get("box") if isinstance(item.get("box"), dict) else item

        if not isinstance(source, dict):
            return None

        if {"x_min", "y_min", "width", "height"}.issubset(source.keys()):
            x_min = int(float(source.get("x_min", 0) or 0))
            y_min = int(float(source.get("y_min", 0) or 0))
            width = int(float(source.get("width", 0) or 0))
            height = int(float(source.get("height", 0) or 0))
            if width > 0 and height > 0:
                return {
                    "x_min": x_min,
                    "y_min": y_min,
                    "width": width,
                    "height": height,
                }

        if {"x_min", "y_min", "x_max", "y_max"}.issubset(source.keys()):
            x_min = int(float(source.get("x_min", 0) or 0))
            y_min = int(float(source.get("y_min", 0) or 0))
            x_max = int(float(source.get("x_max", 0) or 0))
            y_max = int(float(source.get("y_max", 0) or 0))
            width = max(0, x_max - x_min)
            height = max(0, y_max - y_min)
            if width > 0 and height > 0:
                return {
                    "x_min": x_min,
                    "y_min": y_min,
                    "width": width,
                    "height": height,
                }

        return None

    @staticmethod
    def _build_unknown_key(box: dict[str, int] | None) -> str | None:
        if box is None:
            return None

        x_min = max(0, int(box.get("x_min", 0)))
        y_min = max(0, int(box.get("y_min", 0)))
        width = max(0, int(box.get("width", 0)))
        height = max(0, int(box.get("height", 0)))
        if width <= 0 or height <= 0:
            return None

        q = 20
        return f"{x_min // q}:{y_min // q}:{width // q}:{height // q}"

    @staticmethod
    def _encode_unknown_face(frame: Any, box: dict[str, int] | None) -> str | None:
        if frame is None or not hasattr(frame, "shape"):
            return None

        target = frame
        if box is not None:
            h, w = frame.shape[:2]
            x_min = max(0, int(box.get("x_min", 0)))
            y_min = max(0, int(box.get("y_min", 0)))
            width = max(0, int(box.get("width", 0)))
            height = max(0, int(box.get("height", 0)))

            if width > 0 and height > 0:
                x_max = min(w, x_min + width)
                y_max = min(h, y_min + height)

                pad_x = max(6, int(width * 0.2))
                pad_y = max(6, int(height * 0.2))
                x0 = max(0, x_min - pad_x)
                y0 = max(0, y_min - pad_y)
                x1 = min(w, x_max + pad_x)
                y1 = min(h, y_max + pad_y)

                if x1 > x0 and y1 > y0 and (x1 - x0) >= 24 and (y1 - y0) >= 24:
                    target = frame[y0:y1, x0:x1]

        if target.size == 0:
            return None

        h, w = target.shape[:2]
        max_side = max(h, w)
        max_encoded_side = 640
        if max_side > max_encoded_side:
            scale = max_encoded_side / float(max_side)
            new_w = max(24, int(w * scale))
            new_h = max(24, int(h * scale))
            target = cv2.resize(target, (new_w, new_h), interpolation=cv2.INTER_AREA)

        ok, encoded = cv2.imencode(
            ".jpg",
            target,
            [int(cv2.IMWRITE_JPEG_QUALITY), 88],
        )
        if not ok:
            return None
        return base64.b64encode(encoded.tobytes()).decode("ascii")
