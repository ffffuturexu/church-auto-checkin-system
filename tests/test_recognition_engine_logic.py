from __future__ import annotations

import base64
import threading
import time
from datetime import timedelta

import cv2
import numpy as np

from app.services.recognition_engine import RecognitionEngine, RecognitionHyperParams
from app.core.time_utils import now_local


def _drain_events(engine: RecognitionEngine):
    events = []
    while True:
        event = engine.read_event_nowait()
        if event is None:
            break
        events.append(event)
    return events


def test_extract_box_supports_xyxy_format():
    item = {"box": {"x_min": 10, "y_min": 20, "x_max": 60, "y_max": 95}}

    box = RecognitionEngine._extract_box(item)

    assert box == {"x_min": 10, "y_min": 20, "width": 50, "height": 75}


def test_encode_unknown_face_with_valid_box_returns_non_tiny_image():
    frame = np.full((240, 320, 3), 20, dtype=np.uint8)
    frame[80:160, 100:180] = 220

    box = RecognitionEngine._extract_box(
        {"box": {"x_min": 100, "y_min": 80, "x_max": 180, "y_max": 160}}
    )
    assert box is not None

    image_b64 = RecognitionEngine._encode_unknown_face(frame, box)
    assert image_b64 is not None

    decoded = cv2.imdecode(np.frombuffer(base64.b64decode(image_b64), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    h, w = decoded.shape[:2]
    assert h >= 24
    assert w >= 24
    assert float(decoded.mean()) > 10.0


def test_encode_unknown_face_tiny_box_falls_back_to_full_frame():
    frame = np.full((120, 160, 3), 64, dtype=np.uint8)
    box = {"x_min": 20, "y_min": 20, "width": 1, "height": 1}

    image_b64 = RecognitionEngine._encode_unknown_face(frame, box)
    assert image_b64 is not None

    decoded = cv2.imdecode(np.frombuffer(base64.b64decode(image_b64), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape[0] == 120
    assert decoded.shape[1] == 160


def test_emit_unknown_uses_per_box_cooldown_not_global_lock():
    class DummyClient:
        def recognize_image(self, _image_bytes):
            return {"result": []}

    engine = RecognitionEngine(
        api_client=DummyClient(),
        params=RecognitionHyperParams(unknown_min_face_size=24),
    )
    frame = np.full((160, 220, 3), 90, dtype=np.uint8)

    box_a = {"x_min": 20, "y_min": 30, "width": 40, "height": 40}
    box_b = {"x_min": 130, "y_min": 30, "width": 40, "height": 40}

    engine._emit_unknown(frame, box_a, reason="failed_threshold")
    engine._emit_unknown(frame, box_b, reason="failed_threshold")

    first = engine.read_event_nowait()
    second = engine.read_event_nowait()

    assert first is not None
    assert second is not None
    assert first["event_type"] == "recognition.unknown"
    assert second["event_type"] == "recognition.unknown"


def test_low_similarity_tracks_stranger_without_immediate_unknown_emit():
    class DummyClient:
        def recognize_image(self, _image_bytes):
            return {"result": []}

    engine = RecognitionEngine(
        api_client=DummyClient(),
        params=RecognitionHyperParams(vote_threshold=0.85, pending_min_similarity=0.80, unknown_min_similarity=0.65),
    )
    frame = np.full((140, 200, 3), 90, dtype=np.uint8)
    result = {
        "result": [
            {
                "box": {"x_min": 20, "y_min": 20, "width": 90, "height": 90},
                "subjects": [{"subject": "member-a", "similarity": 0.4367}],
            }
        ]
    }

    engine._handle_recognition_result(frame, result)
    events = _drain_events(engine)

    assert not any(e["event_type"] == "recognition_log" and e["status"] == "failed_threshold" for e in events)
    assert not any(e["event_type"] == "recognition.unknown" for e in events)
    assert any(e["event_type"] == "debug_frame" and e["status"] == "scanning" for e in events)


def test_failed_threshold_in_review_band_emits_unknown():
    class DummyClient:
        def recognize_image(self, _image_bytes):
            return {"result": []}

    engine = RecognitionEngine(
        api_client=DummyClient(),
        params=RecognitionHyperParams(
            vote_threshold=0.85,
            pending_min_similarity=0.70,
            pending_min_frames=1,
            stranger_max_similarity=0.40,
            stranger_min_frames=1,
            stranger_window_sec=0.1,
            vote_window_sec=0.1,
            unknown_min_similarity=0.65,
            unknown_min_face_size=24,
        ),
    )
    frame = np.full((140, 200, 3), 100, dtype=np.uint8)
    result = {
        "result": [
            {
                "box": {"x_min": 20, "y_min": 20, "width": 90, "height": 90},
                "subjects": [{"subject": "member-b", "similarity": 0.72}],
            }
        ]
    }

    engine._handle_recognition_result(frame, result)
    engine._finalize_pending_candidates(now_local() + timedelta(seconds=0.2))
    events = _drain_events(engine)

    pending_events = [e for e in events if e["event_type"] == "recognition.pending"]
    assert len(pending_events) == 1
    assert pending_events[0]["data"]["reason"] in {"vote_timeout", "weak_consensus", "ambiguous_margin"}
    assert pending_events[0]["data"]["subject_id"] == "member-b"
    assert pending_events[0]["data"]["best_similarity"] == 0.72


def test_unknown_event_ignores_small_face_box():
    class DummyClient:
        def recognize_image(self, _image_bytes):
            return {"result": []}

    engine = RecognitionEngine(
        api_client=DummyClient(),
        params=RecognitionHyperParams(unknown_min_face_size=80),
    )
    frame = np.full((120, 160, 3), 80, dtype=np.uint8)
    box = {"x_min": 10, "y_min": 10, "width": 40, "height": 40}

    engine._track_stranger_observation(frame=frame, box=box, similarity=0.2)
    engine._finalize_stranger_candidates(now_local() + timedelta(seconds=2))

    assert engine.read_event_nowait() is None


def test_recognition_engine_parallel_requests_reach_multiple_inflight_workers():
    class SlowClient:
        def __init__(self):
            self._lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def recognize_image(self, _image_bytes):
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.12)
                return {"result": []}
            finally:
                with self._lock:
                    self.active -= 1

    client = SlowClient()
    params = RecognitionHyperParams(frame_skip=1, vote_min_samples=1)
    engine = RecognitionEngine(
        api_client=client,
        params=params,
        recognition_workers=2,
        max_inflight_requests=6,
    )

    engine.start()
    try:
        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        for _ in range(8):
            engine.submit_frame(frame)

        deadline = time.time() + 1.5
        while time.time() < deadline and client.max_active < 2:
            time.sleep(0.02)
    finally:
        engine.stop()

    assert client.max_active >= 2
