from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.services.event_dispatcher as dispatcher_module
from app.core.websocket_manager import WebSocketManager
from app.models.models import AttendanceEvent, AttendanceRecord, Base, Member, RecognitionLog, UnknownFaceCase
from app.routers.websocket import router as websocket_router
from app.services.event_dispatcher import EventDispatcher


class DummyRuntime:
    def read_event_nowait(self):
        return None


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

    app = FastAPI(title="dispatcher-ws-integration")
    app.state.ws_manager = ws_manager
    app.state.event_dispatcher = dispatcher
    app.include_router(websocket_router)

    test_router = APIRouter()

    @test_router.post("/_test/dispatch")
    async def dispatch_event(payload: dict):
        await app.state.event_dispatcher._handle_event(payload)
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

    with session_factory() as db:
        logs = db.execute(select(RecognitionLog)).scalars().all()
        assert len(logs) == 1
        assert str(logs[0].best_subject_id) == "Xu Weilai"

        rows = db.execute(select(UnknownFaceCase)).scalars().all()
        assert len(rows) == 1
        assert rows[0].reason == "orphaned_profile"


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
