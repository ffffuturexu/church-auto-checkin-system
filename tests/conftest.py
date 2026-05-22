from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import get_db
from app.models.models import Base
from app.routers.attendance import router as attendance_router
from app.routers.care import router as care_router
from app.routers.events import router as events_router
from app.routers.face_library import router as face_library_router
from app.routers.members import router as members_router
from app.routers.reception_queue import router as reception_queue_router
from app.services.face_library_service import FaceLibraryService


class FakeCompreFaceClient:
    def __init__(self) -> None:
        self.created_subjects: list[str] = []
        self.added_faces: list[tuple[str, str, str]] = []
        self.deleted_faces: list[str] = []
        self.deleted_subjects: list[str] = []

    def create_subject(self, name: str) -> dict:
        self.created_subjects.append(name)
        return {"status": "ok", "subject": name}

    def add_face_image(self, subject: str, image_path: str) -> dict:
        face_id = f"face-{len(self.added_faces) + 1}"
        self.added_faces.append((subject, image_path, face_id))
        return {"face_id": face_id}

    def delete_face(self, face_id: str) -> dict:
        self.deleted_faces.append(face_id)
        return {"status": "deleted", "face_id": face_id}

    def delete_subject(self, subject: str) -> dict:
        self.deleted_subjects.append(subject)
        return {"status": "deleted", "subject": subject}


class DummyWsManager:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def broadcast_channel_a(self, payload: dict) -> None:
        self.messages.append(payload)


@pytest.fixture()
def api_context(tmp_path: Path):
    db_path = tmp_path / "test.sqlite3"
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

    app = FastAPI(title="test-app")
    app.include_router(members_router)
    app.include_router(events_router)
    app.include_router(attendance_router)
    app.include_router(care_router)
    app.include_router(face_library_router)
    app.include_router(reception_queue_router)

    fake_client = FakeCompreFaceClient()
    app.state.face_library_service = FaceLibraryService(fake_client, str(tmp_path / "faces"))
    app.state.ws_manager = DummyWsManager()

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        yield {
            "client": client,
            "session_factory": testing_session_local,
            "fake_client": fake_client,
            "ws_manager": app.state.ws_manager,
        }

    Base.metadata.drop_all(bind=engine)
    engine.dispose()
