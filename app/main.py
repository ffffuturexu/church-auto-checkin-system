from contextlib import asynccontextmanager
from pathlib import Path

import uuid

from fastapi import FastAPI

from app.api import CompreFaceClient
from app.core.config import settings
from app.core.database import SessionLocal, init_db
from app.models.models import Member
from app.routers.attendance import router as attendance_router
from app.routers.admin import router as admin_router
from app.routers.care import router as care_router
from app.routers.debug import router as debug_router
from app.routers.events import router as events_router
from app.routers.face_library import router as face_library_router
from app.routers.health import router as health_router
from app.routers.index import router as index_router
from app.routers.members import router as members_router
from app.routers.reception import router as reception_router
from app.routers.reception_feed import router as reception_feed_router
from app.routers.reception_queue import router as reception_queue_router
from app.routers.system import router as system_router
from app.routers.websocket import router as websocket_router
from app.core.websocket_manager import WebSocketManager
from app.services.cleanup_service import RecognitionLogCleanupService, ReceptionFeedCleanupService
from app.services.event_dispatcher import EventDispatcher
from app.services.face_library_service import FaceLibraryService
from app.services.runtime_pipeline import RuntimePipeline


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    storage_root = Path(settings.face_storage_dir)
    if not storage_root.is_absolute():
        storage_root = (Path(__file__).resolve().parents[1] / storage_root).resolve()

    app.state.runtime = RuntimePipeline()

    def _resolve_subject_name(subject_id: str) -> str | None:
        try:
            subject_uuid = uuid.UUID(subject_id)
        except ValueError:
            return None
        with SessionLocal() as db:
            member = db.get(Member, subject_uuid)
            if member is None or not member.status:
                return None
            return member.name_chn or member.name

    app.state.runtime.recognition.set_subject_name_resolver(_resolve_subject_name)
    app.state.ws_manager = WebSocketManager()
    app.state.face_storage_dir = str(storage_root)
    app.state.face_library_service = FaceLibraryService(CompreFaceClient(), str(storage_root))
    app.state.event_dispatcher = EventDispatcher(app.state.runtime, app.state.ws_manager)
    app.state.cleanup_service = RecognitionLogCleanupService(retention_days=7, interval_sec=3600)
    app.state.feed_cleanup_service = ReceptionFeedCleanupService(retention_days=14, interval_sec=3600)
    await app.state.event_dispatcher.start()
    await app.state.cleanup_service.start()
    await app.state.feed_cleanup_service.start()
    try:
        yield
    finally:
        await app.state.feed_cleanup_service.stop()
        await app.state.cleanup_service.stop()
        await app.state.event_dispatcher.stop()
        app.state.runtime.stop_stream()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)

    app.include_router(health_router)
    app.include_router(index_router)
    app.include_router(admin_router)
    app.include_router(events_router)
    app.include_router(members_router)
    app.include_router(face_library_router)
    app.include_router(attendance_router)
    app.include_router(care_router)
    app.include_router(debug_router)
    app.include_router(reception_router)
    app.include_router(reception_feed_router)
    app.include_router(reception_queue_router)
    app.include_router(system_router)
    app.include_router(websocket_router)
    return app


app = create_app()
