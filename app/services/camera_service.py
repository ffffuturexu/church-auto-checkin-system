from __future__ import annotations

import os
import queue
import threading
import time
from datetime import datetime
from dataclasses import dataclass
from typing import Any

import cv2

from app.core.time_utils import now_local


@dataclass
class CameraStats:
    frames_captured: int = 0
    reconnect_count: int = 0
    dropped_frames: int = 0
    last_frame_captured_at: datetime | None = None
    last_open_attempt_at: datetime | None = None
    last_error_at: datetime | None = None


class CameraService:
    """Background camera reader service.

    This service isolates blocking cv2 capture calls in a dedicated thread and
    pushes frames into a bounded queue for downstream recognition workers.
    """

    def __init__(
        self,
        source: str | int,
        max_queue_size: int = 3,
        force_tcp: bool = False,
        reconnect_delay_sec: float = 0.5,
    ) -> None:
        self.source = source
        self.force_tcp = force_tcp
        self.reconnect_delay_sec = reconnect_delay_sec

        self.frame_queue: queue.Queue[Any] = queue.Queue(maxsize=max(1, max_queue_size))

        self._cap = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self.stats = CameraStats()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="camera-service", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
        self._release_capture()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())

    def update_source(self, source: str | int) -> None:
        with self._lock:
            self.source = source
            self._release_capture()

    def read_frame_nowait(self):
        try:
            return self.frame_queue.get_nowait()
        except queue.Empty:
            return None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if not self._ensure_capture():
                self.stats.last_open_attempt_at = now_local()
                time.sleep(self.reconnect_delay_sec)
                continue

            try:
                grabbed = self._cap.grab()
                if not grabbed:
                    self._release_capture()
                    time.sleep(0.1)
                    continue

                ok, frame = self._cap.retrieve()
                if not ok:
                    time.sleep(0.02)
                    continue

                self._push_latest_frame(frame)
                self.stats.frames_captured += 1
                self.stats.last_frame_captured_at = now_local()
            except Exception:
                self.stats.last_error_at = now_local()
                self._release_capture()
                time.sleep(0.25)

        self._release_capture()

    def _push_latest_frame(self, frame: Any) -> None:
        if self.frame_queue.full():
            try:
                self.frame_queue.get_nowait()
                self.stats.dropped_frames += 1
            except queue.Empty:
                pass
        try:
            self.frame_queue.put_nowait(frame)
        except queue.Full:
            self.stats.dropped_frames += 1

    def _ensure_capture(self) -> bool:
        if self._cap is not None and self._cap.isOpened():
            return True

        self._release_capture()
        self._cap = self._open_capture()
        if self._cap is not None and self._cap.isOpened():
            return True

        self.stats.reconnect_count += 1
        return False

    def _open_capture(self):
        backend = cv2.CAP_ANY
        is_rtsp = isinstance(self.source, str) and self.source.lower().startswith("rtsp")
        if is_rtsp:
            backend = cv2.CAP_FFMPEG
            if self.force_tcp:
                os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

        try:
            if backend == cv2.CAP_ANY:
                cap = cv2.VideoCapture(self.source)
            else:
                cap = cv2.VideoCapture(self.source, backend)
        except Exception:
            return None

        if cap and cap.isOpened():
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            return cap

        try:
            cap.release()
        except Exception:
            pass
        return None

    def _release_capture(self) -> None:
        if self._cap is None:
            return
        try:
            self._cap.release()
        except Exception:
            pass
        self._cap = None
