from __future__ import annotations

import asyncio
import queue
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.services.event_dispatcher as dispatcher_module
import app.routers.health as health_module
import app.routers.system as system_module
from app.core.websocket_manager import WebSocketManager
from app.models.models import AttendanceEvent, AttendanceRecord, Base, Member, RecognitionLog, UnknownFaceCase
from app.routers.websocket import router as websocket_router
from app.services.event_dispatcher import EventDispatcher


class DummyRuntime:
    def read_event_nowait(self):
        return None

    def read_debug_frame_nowait(self):
        return None

    def state(self):
        return SimpleNamespace(event_queue_size=0, event_queue_max=256)


@pytest.fixture()
def dispatcher_ws_context(tmp_path):
    db_path = tmp_path / "dispatcher_ws_test.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    testing_session_local = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        class_=Session,
    )
    Base.metadata.create_all(bind=engine)

    runtime = DummyRuntime()
    ws_manager = WebSocketManager()
    dispatcher = EventDispatcher(runtime=runtime, ws_manager=ws_manager)

    @asynccontextmanager
    async def lifespan(app):
        await dispatcher.start()
        try:
            yield
        finally:
            await dispatcher.stop()

    app = FastAPI(title="dispatcher-ws-integration", lifespan=lifespan)
    app.state.ws_manager = ws_manager
    app.state.runtime = runtime
    app.state.event_dispatcher = dispatcher
    app.include_router(websocket_router)
    app.include_router(health_module.router)
    app.include_router(system_module.router)

    test_router = APIRouter()

    @test_router.post("/_test/dispatch")
    async def dispatch_event(payload: dict):
        await app.state.event_dispatcher._handle_event(payload)
        await app.state.event_dispatcher._broadcast_queue.join()
        return {"status": "ok"}

    app.include_router(test_router)

    original_session_local = dispatcher_module.SessionLocal
    dispatcher_module.SessionLocal = testing_session_local

    with TestClient(app) as client:
        yield {
            "client": client,
            "session_factory": testing_session_local,
        }

    dispatcher_module.SessionLocal = original_session_local
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _seed_member_profile(session_factory, name: str = "Dispatch Member") -> tuple[str, str, str]:
    with session_factory() as db:
        member = Member(name=name, group=None, status=True)
        db.add(member)
        db.commit()
        db.refresh(member)
        return str(member.id), str(member.id), member.name


def test_dispatch_checkin_persists_and_pushes_channel_a(dispatcher_ws_context):
    client = dispatcher_ws_context["client"]
    session_factory = dispatcher_ws_context["session_factory"]
    member_id, subject_id, member_name = _seed_member_profile(session_factory)

    with client.websocket_connect("/ws/channel-a") as ws:
        payload = {
            "event_type": "check_in",
            "subject_id": subject_id,
            "similarity": 0.93,
            "timestamp": "2026-04-05T09:30:00",
            "method": "auto_face",
        }
        resp = client.post("/_test/dispatch", json=payload)
        assert resp.status_code == 200

        pushed = ws.receive_json()
        assert pushed["event_type"] == "check_in"
        assert pushed["persist_status"] == "ok"
        assert pushed["member_id"] == member_id
        assert pushed["member_name"] == member_name
        assert "attendance_record_id" in pushed

    with session_factory() as db:
        records = db.execute(select(AttendanceRecord)).scalars().all()
        assert len(records) == 1
        assert str(records[0].member_id) == member_id


def test_dispatch_checkin_non_sunday_is_ignored(dispatcher_ws_context):
    client = dispatcher_ws_context["client"]
    session_factory = dispatcher_ws_context["session_factory"]
    member_id, subject_id, member_name = _seed_member_profile(session_factory)

    with client.websocket_connect("/ws/channel-a") as ws:
        payload = {
            "event_type": "check_in",
            "subject_id": subject_id,
            "similarity": 0.91,
            "timestamp": "2026-04-06T09:30:00",
            "method": "auto_face",
        }
        resp = client.post("/_test/dispatch", json=payload)
        assert resp.status_code == 200

        pushed = ws.receive_json()
        assert pushed["event_type"] == "check_in_ignored"
        assert pushed["persist_status"] == "non_sunday"
        assert pushed["member_id"] == member_id
        assert pushed["member_name"] == member_name

    with session_factory() as db:
        records = db.execute(select(AttendanceRecord)).scalars().all()
        assert len(records) == 0


def test_dispatch_checkin_duplicate_same_event_is_ignored(dispatcher_ws_context):
    client = dispatcher_ws_context["client"]
    session_factory = dispatcher_ws_context["session_factory"]
    member_id, subject_id, member_name = _seed_member_profile(session_factory)

    with client.websocket_connect("/ws/channel-a") as ws:
        payload = {
            "event_type": "check_in",
            "subject_id": subject_id,
            "similarity": 0.95,
            "timestamp": "2026-04-05T09:30:00",
            "method": "auto_face",
        }
        first = client.post("/_test/dispatch", json=payload)
        assert first.status_code == 200
        first_pushed = ws.receive_json()
        assert first_pushed["event_type"] == "check_in"
        assert first_pushed["persist_status"] == "ok"

        second = client.post("/_test/dispatch", json=payload)
        assert second.status_code == 200
        second_pushed = ws.receive_json()
        assert second_pushed["event_type"] == "check_in_ignored"
        assert second_pushed["persist_status"] == "already_checked_in"
        assert second_pushed["member_id"] == member_id
        assert second_pushed["member_name"] == member_name

    with session_factory() as db:
        records = db.execute(select(AttendanceRecord)).scalars().all()
        assert len(records) == 1
        assert str(records[0].member_id) == member_id


def test_dispatch_unknown_face_persists_and_pushes_channel_a(dispatcher_ws_context):
    client = dispatcher_ws_context["client"]
    session_factory = dispatcher_ws_context["session_factory"]

    with client.websocket_connect("/ws/channel-a") as ws:
        payload = {
            "event_type": "unknown_face",
            "timestamp": "2026-04-04T10:00:00",
            "reason": "failed_threshold",
            "image_base64": "ZmFrZS11bmtub3duLWZhY2U=",
        }
        resp = client.post("/_test/dispatch", json=payload)
        assert resp.status_code == 200

        pushed = ws.receive_json()
        assert pushed["event_type"] == "unknown_face"
        assert pushed["queue_status"] == "pending"
        assert pushed.get("case_id")

    with session_factory() as db:
        rows = db.execute(select(UnknownFaceCase)).scalars().all()
        assert len(rows) == 1
        assert rows[0].reason == "failed_threshold"


def test_dispatch_unknown_face_persists_top_level_face_image_base64(dispatcher_ws_context):
    client = dispatcher_ws_context["client"]
    session_factory = dispatcher_ws_context["session_factory"]

    with client.websocket_connect("/ws/channel-a") as ws:
        payload = {
            "event_type": "unknown_face",
            "timestamp": "2026-04-04T10:00:00",
            "reason": "failed_threshold",
            "face_image_base64": "ZmFrZS10b3AtbGV2ZWwtZmFjZQ==",
        }
        resp = client.post("/_test/dispatch", json=payload)
        assert resp.status_code == 200

        pushed = ws.receive_json()
        assert pushed["event_type"] == "unknown_face"
        assert pushed["queue_status"] == "pending"
        assert pushed.get("case_id")

    with session_factory() as db:
        rows = db.execute(select(UnknownFaceCase)).scalars().all()
        assert len(rows) == 1
        assert rows[0].image_base64 == "ZmFrZS10b3AtbGV2ZWwtZmFjZQ=="


def test_dispatch_unknown_face_after_successful_checkin_is_suppressed(dispatcher_ws_context):
    client = dispatcher_ws_context["client"]
    session_factory = dispatcher_ws_context["session_factory"]
    member_id, subject_id, _ = _seed_member_profile(session_factory, name="CheckedIn Member")

    checkin_payload = {
        "event_type": "check_in",
        "subject_id": subject_id,
        "similarity": 0.97,
        "timestamp": "2026-04-05T09:30:00",
        "method": "auto_face",
    }
    checkin_resp = client.post("/_test/dispatch", json=checkin_payload)
    assert checkin_resp.status_code == 200

    unknown_payload = {
        "event_type": "unknown_face",
        "timestamp": "2026-04-05T09:35:00",
        "reason": "failed_threshold",
        "image_base64": "ZmFrZS11bmtub3duLWZhY2U=",
        "best_subject_id": subject_id,
        "similarity": 0.72,
    }
    unknown_resp = client.post("/_test/dispatch", json=unknown_payload)
    assert unknown_resp.status_code == 200

    with session_factory() as db:
        records = db.execute(select(AttendanceRecord)).scalars().all()
        assert len(records) == 1
        assert str(records[0].member_id) == member_id

        rows = db.execute(select(UnknownFaceCase)).scalars().all()
        assert len(rows) == 0


def test_dispatch_failed_threshold_then_unknown_creates_single_recognition_log(dispatcher_ws_context):
    client = dispatcher_ws_context["client"]
    session_factory = dispatcher_ws_context["session_factory"]
    member_id, subject_id, _ = _seed_member_profile(session_factory, name="Member A")
    second_member_id, second_subject_id, _ = _seed_member_profile(session_factory, name="Member B")

    with client.websocket_connect("/ws/channel-a") as ws:
        recognition_payload = {
            "event_type": "recognition_log",
            "timestamp": "2026-04-07T15:07:39",
            "status": "failed_threshold",
            "best_subject_id": subject_id,
            "similarity": 0.683,
            "second_subject_id": second_subject_id,
            "second_similarity": 0.611,
        }
        resp = client.post("/_test/dispatch", json=recognition_payload)
        assert resp.status_code == 200
        assert ws.receive_json()["event_type"] == "recognition_log"

        unknown_payload = {
            "event_type": "unknown_face",
            "timestamp": "2026-04-07T15:07:39",
            "reason": "failed_threshold",
            "image_base64": "ZmFrZS11bmtub3duLWZhY2U=",
            "best_subject_id": subject_id,
            "similarity": 0.683,
            "second_subject_id": second_subject_id,
            "second_similarity": 0.611,
        }
        resp = client.post("/_test/dispatch", json=unknown_payload)
        assert resp.status_code == 200
        pushed = ws.receive_json()
        assert pushed["event_type"] == "unknown_face"

    with session_factory() as db:
        logs = db.execute(select(RecognitionLog)).scalars().all()
        assert len(logs) == 1
        assert str(logs[0].best_subject_id) == member_id
        assert logs[0].best_subject_name == "Member A"
        assert str(logs[0].second_subject_id) == second_member_id
        assert logs[0].second_subject_name == "Member B"
        assert logs[0].second_similarity == pytest.approx(0.611)

        rows = db.execute(select(UnknownFaceCase)).scalars().all()
        assert len(rows) == 1
        assert rows[0].reason == "failed_threshold"


def test_dispatch_debug_frame_pushes_channel_b(dispatcher_ws_context):
    client = dispatcher_ws_context["client"]

    with client.websocket_connect("/ws/channel-b") as ws:
        payload = {
            "event_type": "debug_frame",
            "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
            "status": "candidate",
            "image_base64": "ZmFrZS1kZWJ1Zy1mcmFtZQ==",
        }
        resp = client.post("/_test/dispatch", json=payload)
        assert resp.status_code == 200

        pushed = ws.receive_json()
        assert pushed["event_type"] == "debug_frame"
        assert pushed["status"] == "candidate"
        assert pushed["image_base64"] == "ZmFrZS1kZWJ1Zy1mcmFtZQ=="


def test_dispatch_checkin_profile_not_found_downgrades_to_unknown_queue(dispatcher_ws_context):
    client = dispatcher_ws_context["client"]
    session_factory = dispatcher_ws_context["session_factory"]

    with client.websocket_connect("/ws/channel-a") as ws:
        payload = {
            "event_type": "check_in",
            "subject_id": "Xu Weilai",
            "similarity": 0.98,
            "timestamp": "2026-04-04T11:00:00",
            "method": "auto_face",
        }
        resp = client.post("/_test/dispatch", json=payload)
        assert resp.status_code == 200

        pushed = ws.receive_json()
        assert pushed["event_type"] == "unknown_face"
        assert pushed["persist_status"] == "profile_not_found"
        assert pushed["reason"] == "orphaned_profile"
        assert pushed["queue_status"] == "pending"
        assert pushed.get("case_id")
        assert pushed["data"]["image_available"] is False

    with session_factory() as db:
        logs = db.execute(select(RecognitionLog)).scalars().all()
        assert len(logs) == 1
        assert str(logs[0].best_subject_id) == "Xu Weilai"

        rows = db.execute(select(UnknownFaceCase)).scalars().all()
        assert len(rows) == 1
        assert rows[0].reason == "orphaned_profile"
        assert rows[0].image_base64 == dispatcher_module.PLACEHOLDER_PNG_BASE64


def test_dispatch_checkin_splits_zh_and_it_by_15_clock(dispatcher_ws_context):
    client = dispatcher_ws_context["client"]
    session_factory = dispatcher_ws_context["session_factory"]
    member_id, subject_id, _ = _seed_member_profile(session_factory, name="Split Member")

    morning = {
        "event_type": "check_in",
        "subject_id": subject_id,
        "similarity": 0.95,
        "timestamp": "2026-04-12T14:30:00+02:00",
        "method": "auto_face",
    }
    evening = {
        "event_type": "check_in",
        "subject_id": subject_id,
        "similarity": 0.96,
        "timestamp": "2026-04-12T16:30:00+02:00",
        "method": "auto_face",
    }

    first = client.post("/_test/dispatch", json=morning)
    assert first.status_code == 200
    second = client.post("/_test/dispatch", json=evening)
    assert second.status_code == 200

    with session_factory() as db:
        records = db.execute(select(AttendanceRecord)).scalars().all()
        assert len(records) == 2
        assert all(str(item.member_id) == member_id for item in records)

        events = db.execute(
            select(AttendanceEvent).where(AttendanceEvent.event_date == datetime(2026, 4, 12).date())
        ).scalars().all()
        names = {row.event_name for row in events}
        assert "主日崇拜（中文） 2026-04-12" in names
        assert "主日崇拜（意语） 2026-04-12" in names


def test_dispatch_checkin_first_sunday_after_15_stays_zh(dispatcher_ws_context):
    client = dispatcher_ws_context["client"]
    session_factory = dispatcher_ws_context["session_factory"]
    _, subject_id, _ = _seed_member_profile(session_factory, name="First Sunday Member")

    payload = {
        "event_type": "check_in",
        "subject_id": subject_id,
        "similarity": 0.95,
        "timestamp": "2026-04-05T16:30:00+02:00",
        "method": "auto_face",
    }
    resp = client.post("/_test/dispatch", json=payload)
    assert resp.status_code == 200

    with session_factory() as db:
        events = db.execute(
            select(AttendanceEvent).where(AttendanceEvent.event_date == datetime(2026, 4, 5).date())
        ).scalars().all()
        names = {row.event_name for row in events}
        assert names == {"主日崇拜（中文） 2026-04-05"}


class QueuedRuntime(DummyRuntime):
    def __init__(self, events):
        self.events = queue.Queue()
        for event in events:
            self.events.put(event)

    def read_event_nowait(self):
        try:
            return self.events.get_nowait()
        except queue.Empty:
            return None


def test_dispatcher_continues_after_event_failure(monkeypatch, caplog):
    async def run():
        runtime = QueuedRuntime([
            {"event_type": "bad", "event_id": "bad-id"},
            {"event_type": "good", "event_id": "good-id"},
        ])
        dispatcher = EventDispatcher(runtime, WebSocketManager(), poll_interval_sec=0.001)
        processed = asyncio.Event()

        async def handle(event):
            if event["event_type"] == "bad":
                raise ValueError("bad event")
            processed.set()

        monkeypatch.setattr(dispatcher, "_handle_event", handle)
        await dispatcher.start()
        try:
            await asyncio.wait_for(processed.wait(), 2)
            assert dispatcher.is_running()
            assert dispatcher.stats()["event_failures_by_type"] == {"bad": 1}
        finally:
            await dispatcher.stop()

    asyncio.run(run())
    assert "type=bad id=bad-id" in caplog.text


def test_supervisor_restarts_crashed_dispatcher(monkeypatch, caplog):
    class FailingRuntime(QueuedRuntime):
        def __init__(self):
            super().__init__([{"event_type": "good"}])
            self.failed = False

        def read_event_nowait(self):
            if not self.failed:
                self.failed = True
                raise RuntimeError("queue unavailable")
            return super().read_event_nowait()

    async def run():
        dispatcher = EventDispatcher(FailingRuntime(), WebSocketManager(), poll_interval_sec=0.001)
        processed = asyncio.Event()

        async def handle(event):
            processed.set()

        monkeypatch.setattr(dispatcher, "_handle_event", handle)
        await dispatcher.start()
        try:
            await asyncio.wait_for(processed.wait(), 2)
            assert dispatcher.stats()["dispatcher_restarts"] == 1
            assert dispatcher.is_running()
        finally:
            await dispatcher.stop()
        assert dispatcher.stats()["dispatcher_restarts"] == 1
        assert not dispatcher.is_running()

    asyncio.run(run())
    assert "event dispatcher crashed; restarting" in caplog.text


def test_database_thread_and_slow_broadcast_do_not_block_consumption(monkeypatch):
    async def run():
        runtime = QueuedRuntime([
            {"event_type": "check_in", "event_id": "first"},
            {"event_type": "check_in", "event_id": "second"},
        ])
        broadcast_started = asyncio.Event()
        release_broadcast = asyncio.Event()
        database_started = asyncio.Event()
        database_done = asyncio.Event()
        release_database = threading.Event()
        loop = asyncio.get_running_loop()
        processed = []

        class SlowWebSocketManager:
            async def broadcast_channel_a(self, payload):
                broadcast_started.set()
                await release_broadcast.wait()

            async def broadcast_channel_queue(self, payload):
                pass

            async def broadcast_channel_b(self, payload):
                pass

        dispatcher = EventDispatcher(runtime, SlowWebSocketManager(), poll_interval_sec=0.001)

        def persist(event):
            processed.append(event["event_id"])
            if event["event_id"] == "first":
                loop.call_soon_threadsafe(database_started.set)
                if not release_database.wait(2):
                    raise TimeoutError("test database was not released")
            else:
                loop.call_soon_threadsafe(database_done.set)
            return event

        monkeypatch.setattr(dispatcher, "_persist_checkin_event", persist)
        await dispatcher.start()
        try:
            await asyncio.wait_for(database_started.wait(), 2)
            release_database.set()
            await asyncio.wait_for(broadcast_started.wait(), 2)
            await asyncio.wait_for(database_done.wait(), 2)
            assert processed == ["first", "second"]
            assert not release_broadcast.is_set()
        finally:
            release_database.set()
            release_broadcast.set()
            await dispatcher.stop()

    asyncio.run(run())


def test_slow_debug_broadcast_does_not_block_business_events():
    class FrameRuntime(QueuedRuntime):
        def __init__(self):
            super().__init__([])
            self.frame = {"event_type": "debug_frame"}

        def read_debug_frame_nowait(self):
            frame, self.frame = self.frame, None
            return frame

    async def run():
        runtime = FrameRuntime()
        debug_started = asyncio.Event()
        release_debug = asyncio.Event()
        business_received = asyncio.Event()

        class SlowDebugManager:
            async def broadcast_channel_a(self, payload):
                business_received.set()

            async def broadcast_channel_b(self, payload):
                debug_started.set()
                await release_debug.wait()

            async def broadcast_channel_queue(self, payload):
                pass

        dispatcher = EventDispatcher(runtime, SlowDebugManager(), poll_interval_sec=0.001)
        await dispatcher.start()
        try:
            await asyncio.wait_for(debug_started.wait(), 2)
            runtime.events.put({"event_type": "hyperparams_updated"})
            await asyncio.wait_for(business_received.wait(), 2)
            assert not release_debug.is_set()
        finally:
            release_debug.set()
            await dispatcher.stop()

    asyncio.run(run())


def test_broadcast_queue_overflow_is_counted(caplog):
    async def run():
        dispatcher = EventDispatcher(DummyRuntime(), WebSocketManager(), poll_interval_sec=0.001)
        for _ in range(257):
            dispatcher._enqueue_broadcast({"event_type": "recognition.success"})
        assert dispatcher.stats()["broadcast_queue_size"] == 256
        assert dispatcher.stats()["broadcast_drops_by_type"] == {"recognition.success": 1}
        await dispatcher.start()
        await dispatcher.stop()

    asyncio.run(run())
    assert "broadcast queue dropped event: type=recognition.success" in caplog.text
    assert "broadcast queue at or above 80%" in caplog.text


def test_shutdown_waits_for_database_and_drains_queued_events(monkeypatch):
    async def run():
        runtime = QueuedRuntime([
            {"event_type": "check_in", "event_id": "first"},
            {"event_type": "check_in", "event_id": "second"},
        ])
        dispatcher = EventDispatcher(runtime, WebSocketManager(), poll_interval_sec=0.001)
        database_started = asyncio.Event()
        release_database = threading.Event()
        loop = asyncio.get_running_loop()
        processed = []

        def persist(event):
            processed.append(event["event_id"])
            if event["event_id"] == "first":
                loop.call_soon_threadsafe(database_started.set)
                if not release_database.wait(2):
                    raise TimeoutError("test database was not released")
            return event

        monkeypatch.setattr(dispatcher, "_persist_checkin_event", persist)
        await dispatcher.start()
        try:
            await asyncio.wait_for(database_started.wait(), 2)
            stopping = asyncio.create_task(dispatcher.stop())
            await asyncio.sleep(0.01)
            assert not stopping.done()
            release_database.set()
            await asyncio.wait_for(stopping, 2)
            assert processed == ["first", "second"]
            assert dispatcher.stats()["broadcast_queue_size"] == 0
        finally:
            release_database.set()
            if dispatcher.is_running():
                await dispatcher.stop()

    asyncio.run(run())


def test_cancelled_dispatcher_finishes_database_write_before_restart(monkeypatch):
    async def run():
        runtime = QueuedRuntime([
            {"event_type": "check_in", "event_id": "first"},
            {"event_type": "check_in", "event_id": "second"},
        ])
        dispatcher = EventDispatcher(runtime, WebSocketManager(), poll_interval_sec=0.001)
        database_started = asyncio.Event()
        second_started = asyncio.Event()
        release_database = threading.Event()
        loop = asyncio.get_running_loop()
        processed = []

        def persist(event):
            processed.append(event["event_id"])
            if event["event_id"] == "first":
                loop.call_soon_threadsafe(database_started.set)
                if not release_database.wait(2):
                    raise TimeoutError("test database was not released")
            else:
                loop.call_soon_threadsafe(second_started.set)
            return event

        monkeypatch.setattr(dispatcher, "_persist_checkin_event", persist)
        await dispatcher.start()
        try:
            await asyncio.wait_for(database_started.wait(), 2)
            dispatcher._task.cancel()
            await asyncio.sleep(0.15)
            assert processed == ["first"]
            assert dispatcher.stats()["dispatcher_restarts"] == 0
            release_database.set()
            await asyncio.wait_for(second_started.wait(), 2)
            assert processed == ["first", "second"]
            assert dispatcher.stats()["dispatcher_restarts"] == 1
        finally:
            release_database.set()
            await dispatcher.stop()

    asyncio.run(run())


def test_websocket_send_failure_is_reported(caplog):
    async def run():
        class FailingSocket:
            async def send_json(self, payload):
                raise RuntimeError("disconnected")

        manager = WebSocketManager()
        manager._channel_a.add(FailingSocket())
        await manager.broadcast_channel_a({"event_type": "check_in"})
        stats = await manager.get_stats()
        assert stats.channel_a_connections == 0
        assert stats.send_failures_by_channel == {"a": 1}

    asyncio.run(run())
    assert "websocket send failures: channel=a" in caplog.text


def test_health_and_self_check_expose_dispatcher_metrics(dispatcher_ws_context, monkeypatch):
    monkeypatch.setattr(system_module, "SessionLocal", dispatcher_ws_context["session_factory"])
    monkeypatch.setattr(health_module, "get_process_metrics", lambda: SimpleNamespace())
    client = dispatcher_ws_context["client"]

    health = client.get("/health").json()
    assert health["event_dispatcher"]["running"] is True
    assert health["event_dispatcher"]["broadcast_queue_max"] == 256
    assert health["websocket"]["send_failures_by_channel"] == {}
    self_check = client.get("/system/self-check").json()
    assert self_check["checks"]["event_dispatcher"]["state"]["dispatcher_restarts"] == 0
    assert self_check["checks"]["runtime"]["state"]["event_queue_size"] == 0
