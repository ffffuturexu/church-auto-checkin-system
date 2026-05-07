from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text

from app.core.database import SessionLocal

from app.services.runtime_pipeline import RuntimePipeline

router = APIRouter(prefix="/system", tags=["system"])


class StreamStartRequest(BaseModel):
    source: str | int | None = None


class HyperParamsUpdateRequest(BaseModel):
    threshold: float | None = None
    margin: float | None = None
    frame_skip: int | None = None
    dedupe_seconds: int | None = None
    vote_min_samples: int | None = None
    vote_window_sec: float | None = None
    vote_ratio: float | None = None
    unknown_min_similarity: float | None = None
    unknown_min_face_size: int | None = None


def _get_runtime(request: Request) -> RuntimePipeline:
    return request.app.state.runtime


@router.get("/self-check")
async def self_check(request: Request) -> dict[str, Any]:
    runtime = _get_runtime(request)
    ws_manager = getattr(request.app.state, "ws_manager", None)
    dispatcher = getattr(request.app.state, "event_dispatcher", None)
    cleanup_service = getattr(request.app.state, "cleanup_service", None)
    feed_cleanup_service = getattr(request.app.state, "feed_cleanup_service", None)

    db_ok = False
    db_error = None
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        db_error = str(exc)

    ws_stats = None
    if ws_manager is not None:
        ws_stats = (await ws_manager.get_stats()).__dict__

    checks = {
        "database": {"ok": db_ok, "error": db_error},
        "runtime": {
            "ok": True,
            "state": runtime.state().__dict__,
        },
        "websocket": {
            "ok": ws_manager is not None,
            "state": ws_stats,
        },
        "event_dispatcher": {
            "ok": bool(dispatcher and dispatcher.is_running()),
        },
        "cleanup": {
            "ok": cleanup_service is not None,
            "state": cleanup_service.state().__dict__ if cleanup_service is not None else None,
        },
        "feed_cleanup": {
            "ok": feed_cleanup_service is not None,
            "state": feed_cleanup_service.state().__dict__ if feed_cleanup_service is not None else None,
        },
    }

    overall_ok = all(item.get("ok", False) for item in checks.values())
    return {
        "status": "ok" if overall_ok else "degraded",
        "checks": checks,
    }


@router.get("/status")
async def system_status(request: Request) -> dict[str, Any]:
    runtime = _get_runtime(request)
    state = runtime.state().__dict__
    ws_manager = getattr(request.app.state, "ws_manager", None)
    cleanup_service = getattr(request.app.state, "cleanup_service", None)
    feed_cleanup_service = getattr(request.app.state, "feed_cleanup_service", None)

    payload: dict[str, Any] = {"status": "ok", "runtime": state}
    if ws_manager is not None:
        payload["websocket"] = (await ws_manager.get_stats()).__dict__
    if cleanup_service is not None:
        payload["cleanup"] = cleanup_service.state().__dict__
    if feed_cleanup_service is not None:
        payload["feed_cleanup"] = feed_cleanup_service.state().__dict__
    return payload


@router.post("/stream/start")
def start_stream(payload: StreamStartRequest, request: Request) -> dict[str, Any]:
    runtime = _get_runtime(request)
    source = payload.source
    if isinstance(source, str):
        raw = source.strip()
        if raw.isdigit():
            source = int(raw)
        else:
            source = raw

    runtime.start_stream(source=source)
    return {"status": "started", "runtime": runtime.state().__dict__}


@router.post("/stream/stop")
def stop_stream(request: Request) -> dict[str, Any]:
    runtime = _get_runtime(request)
    runtime.stop_stream()
    return {"status": "stopped", "runtime": runtime.state().__dict__}


@router.put("/hyperparameters")
def update_hyperparameters(payload: HyperParamsUpdateRequest, request: Request) -> dict[str, Any]:
    runtime = _get_runtime(request)
    updated = runtime.update_hyperparams(**payload.model_dump(exclude_none=True))
    return {"status": "updated", "hyperparameters": updated}
