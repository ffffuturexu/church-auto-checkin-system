from __future__ import annotations

import base64
import queue
import threading
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

import cv2
import requests

from app.api import CompreFaceClient
from app.config import settings
from app.core.time_utils import now_local


@dataclass
class RecognitionHyperParams:
    threshold: float = 0.70
    margin: float = 0.18
    dedupe_seconds: int = 60
    frame_skip: int = 2
    vote_window_sec: float = 1.5
    vote_min_samples: int = 5
    vote_ratio: float = 0.65
    unknown_min_similarity: float = 0.65
    unknown_min_face_size: int = 64


class RecognitionEngine:
    """Background recognition engine with voting and dedupe.

    Input path:
    - `submit_frame(frame)` accepts raw OpenCV frames.

    Output path:
    - `event_queue` emits structured dict events for control-plane consumption.
    """

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
        self.params = params or RecognitionHyperParams(
            threshold=settings.THRESHOLD,
            margin=settings.MARGIN,
            dedupe_seconds=settings.DEDUPE_SECONDS,
            frame_skip=settings.FRAME_SKIP,
            vote_window_sec=settings.VOTE_WINDOW_SEC,
            vote_min_samples=settings.VOTE_MIN_SAMPLES,
            vote_ratio=settings.VOTE_RATIO,
            unknown_min_similarity=settings.UNKNOWN_MIN_SIMILARITY,
            unknown_min_face_size=settings.UNKNOWN_MIN_FACE_SIZE,
        )

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
        self._vote_buffer: deque[tuple[datetime, str, float]] = deque()
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
            }

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
        self._last_result_at = now_local()
        if not detections:
            self._emit_debug_frame(frame, box=None, best_subject_id="No Face", similarity=0.0, status="scanning")
            return

        for item in detections:
            box = self._extract_box(item)
            subjects = item.get("subjects", [])
            best, second = self._pick_best_two(subjects)

            if best is None:
                self._emit_unknown(frame, box, reason="unknown")
                self._emit_debug_frame(frame, box=box, best_subject_id="Unknown", similarity=0.0, status="unknown")
                continue

            best_subject = str(best.get("subject", "")).strip()
            best_similarity = float(best.get("similarity", 0.0) or 0.0)
            second_subject = None
            second_similarity = 0.0
            if second is not None:
                second_subject = second.get("subject")
                second_similarity = float(second.get("similarity", 0.0) or 0.0)

            if not best_subject:
                self._emit_unknown(frame, box, reason="missing_subject")
                continue

            threshold = float(self.params.threshold)
            margin = float(self.params.margin)

            if best_similarity < threshold:
                self._emit_log(
                    status="failed_threshold",
                    best_subject_id=best_subject,
                    similarity=best_similarity,
                    second_subject_id=second_subject,
                    second_similarity=second_similarity,
                )
                should_review = self._should_review_failed_threshold(best_similarity)
                if should_review:
                    self._emit_unknown(
                        frame,
                        box,
                        reason="failed_threshold",
                        best_subject_id=best_subject,
                        similarity=best_similarity,
                        second_subject_id=second_subject,
                        second_similarity=second_similarity,
                    )
                self._emit_debug_frame(
                    frame,
                    box=box,
                    best_subject_id=best_subject,
                    similarity=best_similarity,
                    status="failed_threshold" if should_review else "below_review_threshold",
                )
                continue

            if (best_similarity - second_similarity) < margin:
                self._emit_log(
                    status="failed_margin",
                    best_subject_id=best_subject,
                    similarity=best_similarity,
                    second_subject_id=second_subject,
                    second_similarity=second_similarity,
                )
                self._emit_unknown(
                    frame,
                    box,
                    reason="failed_margin",
                    best_subject_id=best_subject,
                    similarity=best_similarity,
                    second_subject_id=second_subject,
                    second_similarity=second_similarity,
                )
                self._emit_debug_frame(
                    frame,
                    box=box,
                    best_subject_id=best_subject,
                    similarity=best_similarity,
                    status="failed_margin",
                )
                continue

            self._add_vote_and_maybe_checkin(
                best_subject,
                best_similarity,
                frame=frame,
                box=box,
                second_subject_id=second_subject,
                second_similarity=second_similarity,
            )
            self._emit_debug_frame(
                frame,
                box=box,
                best_subject_id=best_subject,
                similarity=best_similarity,
                status="candidate",
            )

    def _add_vote_and_maybe_checkin(
        self,
        subject: str,
        similarity: float,
        frame: Any,
        box: dict[str, int] | None,
        second_subject_id: str | None = None,
        second_similarity: float | None = None,
    ) -> None:
        now = now_local()
        window = max(0.1, float(self.params.vote_window_sec))
        cutoff = now - timedelta(seconds=window)

        self._vote_buffer.append((now, subject, similarity))
        while self._vote_buffer and self._vote_buffer[0][0] < cutoff:
            self._vote_buffer.popleft()

        min_samples = max(1, int(self.params.vote_min_samples))
        if len(self._vote_buffer) < min_samples:
            return

        counts = Counter(item[1] for item in self._vote_buffer)
        top_subject, top_count = counts.most_common(1)[0]
        ratio = top_count / max(1, len(self._vote_buffer))
        if ratio < float(self.params.vote_ratio):
            return

        top_sims = [item[2] for item in self._vote_buffer if item[1] == top_subject]
        final_similarity = max(top_sims) if top_sims else similarity
        self._vote_buffer.clear()

        if top_subject == subject:
            self._commit_checkin(
                top_subject,
                final_similarity,
                frame=frame,
                box=box,
                second_subject_id=second_subject_id,
                second_similarity=second_similarity,
            )
            return

        self._commit_checkin(
            top_subject,
            final_similarity,
            frame=None,
            box=None,
            second_subject_id=None,
            second_similarity=None,
        )

    def _commit_checkin(
        self,
        subject_id: str,
        similarity: float,
        frame: Any | None,
        box: dict[str, int] | None,
        second_subject_id: str | None = None,
        second_similarity: float | None = None,
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
        payload: dict[str, Any] = {
            "subject_id": subject_id,
            "similarity": similarity,
            "timestamp": timestamp,
            "method": "auto_face",
        }
        if face_image_base64:
            payload["face_image_base64"] = face_image_base64

        self._emit_event("check_in", **payload)

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
        if not self._passes_unknown_box_filter(box):
            return

        now = now_local()
        key = self._build_unknown_key(box)
        if key is not None:
            last_seen = self._unknown_last_seen.get(key)
            if last_seen is not None:
                elapsed = (now - last_seen).total_seconds()
                if elapsed < self._unknown_cooldown_sec:
                    return

        image_b64 = self._encode_unknown_face(frame, box)
        if image_b64 is None:
            return

        if key is not None:
            self._unknown_last_seen[key] = now
            stale_before = now - timedelta(seconds=self._unknown_cooldown_sec * 4)
            stale_keys = [k for k, ts in self._unknown_last_seen.items() if ts < stale_before]
            for stale_key in stale_keys:
                self._unknown_last_seen.pop(stale_key, None)

        self._emit_event(
            "unknown_face",
            timestamp=now.isoformat(timespec="seconds"),
            reason=reason,
            image_base64=image_b64,
            best_subject_id=best_subject_id,
            similarity=similarity,
            second_subject_id=second_subject_id,
            second_similarity=second_similarity,
        )

    def _should_review_failed_threshold(self, similarity: float | None) -> bool:
        if similarity is None:
            return False
        floor = float(self.params.unknown_min_similarity)
        return float(similarity) >= max(0.0, min(1.0, floor))

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

    def _emit_debug_frame(
        self,
        frame: Any,
        box: dict[str, int] | None,
        best_subject_id: str,
        similarity: float,
        status: str,
    ) -> None:
        self._last_debug_frame_at = now_local()
        debug_frame = frame.copy()
        if box is not None:
            x_min = int(box.get("x_min", 0))
            y_min = int(box.get("y_min", 0))
            width = int(box.get("width", 0))
            height = int(box.get("height", 0))
            x_max = x_min + max(0, width)
            y_max = y_min + max(0, height)
            cv2.rectangle(debug_frame, (x_min, y_min), (x_max, y_max), (0, 255, 0), 2)
            cv2.putText(
                debug_frame,
                f"{best_subject_id} {similarity:.3f}",
                (x_min, max(20, y_min - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )

        resized = cv2.resize(debug_frame, (640, 360), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(
            ".jpg",
            resized,
            [int(cv2.IMWRITE_JPEG_QUALITY), 50],
        )
        if not ok:
            return

        self._emit_event(
            "debug_frame",
            timestamp=now_local().isoformat(timespec="seconds"),
            status=status,
            image_base64=base64.b64encode(encoded.tobytes()).decode("ascii"),
        )

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

        # Quantization keeps nearby jitter frames mapped to the same transient key.
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
        # Keep more detail for reception review; only downscale very large crops.
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
