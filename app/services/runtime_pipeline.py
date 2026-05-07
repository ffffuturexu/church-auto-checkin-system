from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from app.config import settings
from app.services.camera_service import CameraService
from app.services.recognition_engine import RecognitionEngine, RecognitionHyperParams


@dataclass
class RuntimeState:
    stream_running: bool
    camera_running: bool
    recognition_running: bool
    frame_queue_size: int
    event_queue_size: int
    camera_stats: dict[str, Any]
    hyperparams: dict[str, Any]


class RuntimePipeline:
    """Bridge service that connects camera frames to recognition engine."""

    def __init__(self) -> None:
        source = settings.RTSP_URL if settings.RTSP_URL else settings.CAMERA_INDEX
        force_tcp = bool(settings.RTSP_URL and getattr(settings, "RTSP_TCP", True))

        self.camera = CameraService(
            source=source,
            max_queue_size=getattr(settings, "MAX_QUEUE_SIZE", 3),
            force_tcp=force_tcp,
        )
        self.recognition = RecognitionEngine(
            params=RecognitionHyperParams(
                threshold=settings.THRESHOLD,
                margin=settings.MARGIN,
                dedupe_seconds=settings.DEDUPE_SECONDS,
                frame_skip=settings.FRAME_SKIP,
                unknown_min_similarity=settings.UNKNOWN_MIN_SIMILARITY,
                unknown_min_face_size=settings.UNKNOWN_MIN_FACE_SIZE,
            )
        )

        self._pump_thread: threading.Thread | None = None
        self._pump_stop = threading.Event()
        self._lock = threading.Lock()

    def start_stream(self, source: str | int | None = None) -> None:
        with self._lock:
            if source is not None:
                self.camera.update_source(source)
            if self._pump_thread and self._pump_thread.is_alive():
                return
            self._pump_stop.clear()
            self.camera.start()
            self.recognition.start()
            self._pump_thread = threading.Thread(target=self._pump_frames, name="runtime-pump", daemon=True)
            self._pump_thread.start()

    def stop_stream(self) -> None:
        self._pump_stop.set()
        thread = self._pump_thread
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
        self.camera.stop()
        self.recognition.stop()

    def update_hyperparams(self, **kwargs: Any) -> dict[str, Any]:
        updated = self.recognition.update_hyperparams(**kwargs)
        return asdict(updated)

    def read_event_nowait(self) -> dict[str, Any] | None:
        return self.recognition.read_event_nowait()

    def state(self) -> RuntimeState:
        return RuntimeState(
            stream_running=bool(self._pump_thread and self._pump_thread.is_alive()),
            camera_running=self.camera.is_running(),
            recognition_running=self.recognition.is_running(),
            frame_queue_size=self.camera.frame_queue.qsize(),
            event_queue_size=self.recognition.event_queue.qsize(),
            camera_stats=asdict(self.camera.stats),
            hyperparams=self.recognition.get_hyperparams(),
        )

    def _pump_frames(self) -> None:
        while not self._pump_stop.is_set():
            frame = self.camera.read_frame_nowait()
            if frame is None:
                time.sleep(0.01)
                continue
            self.recognition.submit_frame(frame)
