from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text

from app.core.database import SessionLocal
from app.core.process_metrics import get_process_metrics

from app.services.runtime_pipeline import RuntimePipeline
from pathlib import Path
import json
import os
from pydantic import BaseModel

router = APIRouter(prefix="/system", tags=["system"])


class StreamStartRequest(BaseModel):
    source: str | int | None = None


class HyperParamsUpdateRequest(BaseModel):
    absolute_threshold: float | None = None
    vote_threshold: float | None = None
    margin: float | None = None
    frame_skip: int | None = None
    dedupe_seconds: int | None = None
    vote_min_samples: int | None = None
    vote_window_sec: float | None = None
    vote_ratio: float | None = None
    unknown_min_similarity: float | None = None
    unknown_min_face_size: int | None = None
    pending_min_similarity: float | None = None
    pending_min_frames: int | None = None
    stranger_max_similarity: float | None = None
    stranger_window_sec: float | None = None
    stranger_min_frames: int | None = None
    max_finalize_sec: float | None = None
    track_max_age_sec: float | None = None
    eligibility_window_sec: float | None = None
    track_match_iou_threshold: float | None = None
    track_match_center_distance_px: float | None = None


class DebugVideoToggleRequest(BaseModel):
    enabled: bool = True


class DebugOverlayToggleRequest(BaseModel):
    enabled: bool = True


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
    payload["system"] = get_process_metrics().__dict__
    payload["hyperparams"] = runtime.recognition.get_hyperparams()
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


@router.put("/debug-video")
def update_debug_video(payload: DebugVideoToggleRequest, request: Request) -> dict[str, Any]:
    runtime = _get_runtime(request)
    runtime.recognition.set_debug_enabled(payload.enabled)
    return {"status": "updated", "debug_video_enabled": runtime.recognition.is_debug_enabled()}


@router.put("/debug-overlay")
def update_debug_overlay(payload: DebugOverlayToggleRequest, request: Request) -> dict[str, Any]:
    runtime = _get_runtime(request)
    runtime.recognition.set_debug_overlay_enabled(payload.enabled)
    return {"status": "updated", "debug_overlay_enabled": runtime.recognition.is_debug_overlay_enabled()}


@router.post("/restart")
async def restart_system(request: Request) -> dict[str, Any]:
    runtime = _get_runtime(request)
    dispatcher = getattr(request.app.state, "event_dispatcher", None)
    runtime.stop_stream()
    runtime.start_stream()
    if dispatcher is not None and not dispatcher.is_running():
        await dispatcher.start()
    return {"status": "restarted", "runtime": runtime.state().__dict__}


class WeeklyReportConfig(BaseModel):
    smtp_user: str | None = None
    mail_from: str | None = None
    mail_to: str | None = None


def _config_path() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    data_dir = repo_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "weekly_report_config.json"


@router.get("/weekly-report-config")
def get_weekly_report_config(request: Request) -> dict[str, Any]:
    path = _config_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    # fallback to environment variables (do NOT return password)
    return {
        "smtp_user": os.getenv("REPORT_SMTP_USER"),
        "mail_from": os.getenv("REPORT_MAIL_FROM"),
        "mail_to": os.getenv("REPORT_MAIL_TO"),
    }


@router.put("/weekly-report-config")
def put_weekly_report_config(payload: WeeklyReportConfig, request: Request) -> dict[str, Any]:
    data = payload.model_dump(exclude_none=True)
    path = _config_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "saved", "config": data}


# Note: web-based credential editing was removed for security; maintain credentials manually on server.


@router.get("/generate-weekly-brief")
def generate_weekly_brief(request: Request) -> dict[str, Any]:
    # Run the send_weekly_care_brief.py in dry-run mode and return its stdout
    import subprocess, sys

    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "send_weekly_care_brief.py"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    try:
        proc = subprocess.run([sys.executable, str(script), "--dry-run"], capture_output=True, text=True, env=env, timeout=120)
        return {"stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode}
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}


@router.post("/send-weekly-brief")
def send_weekly_brief(request: Request) -> dict[str, Any]:
    import subprocess, sys
    from app.core.database import SessionLocal
    from app.models.models import WeeklyReportLog

    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "send_weekly_care_brief.py"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)

    # Load stored config and inject into env for the subprocess (password must be set in env)
    cfg_path = _config_path()
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}

    if cfg.get("smtp_user"):
        env["REPORT_SMTP_USER"] = cfg.get("smtp_user")
    if cfg.get("mail_from"):
        env["REPORT_MAIL_FROM"] = cfg.get("mail_from")
    if cfg.get("mail_to"):
        env["REPORT_MAIL_TO"] = cfg.get("mail_to")

    # Ensure password present in server environment (do not store password in file)
    if not env.get("REPORT_SMTP_PASS"):
        return {"error": "Missing REPORT_SMTP_PASS in server environment. Set it securely before sending."}

    # First, generate the brief (dry-run) to capture the body
    try:
        gen = subprocess.run([sys.executable, str(script), "--dry-run"], capture_output=True, text=True, env=env, timeout=120)
    except subprocess.TimeoutExpired:
        return {"error": "generation timeout"}

    brief_text = gen.stdout or ""
    if gen.returncode != 0:
        return {"error": "generation_failed", "stderr": gen.stderr or ""}

    # Now run actual sending
    try:
        proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=300)
    except subprocess.TimeoutExpired:
        return {"error": "send timeout"}

    # persist log into DB
    try:
        with SessionLocal() as db:
            recipients = env.get("REPORT_MAIL_TO", "")
            recipients_count = len([r for r in recipients.split(",") if r.strip()])
            sent_count = 1 if proc.returncode == 0 else 0
            subject = f"[关怀周报] 本周聚会概览 — {''}"
            log = WeeklyReportLog(
                recipients=recipients,
                recipients_count=recipients_count,
                sent_count=sent_count,
                subject=subject,
                body=brief_text,
                status=("sent" if sent_count else "failed"),
                note=(proc.stderr or None),
            )
            db.add(log)
            db.commit()
    except Exception as e:
        return {"error": "db_save_failed", "detail": str(e), "stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode}

    return {"stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode}


@router.get("/weekly-report-latest")
def weekly_report_latest(request: Request) -> dict[str, Any]:
    from app.core.database import SessionLocal
    from app.models.models import WeeklyReportLog

    with SessionLocal() as db:
        row = db.query(WeeklyReportLog).order_by(WeeklyReportLog.timestamp.desc()).limit(1).first()
        if not row:
            return {"found": False}
        return {
            "found": True,
            "timestamp": row.timestamp.isoformat(),
            "subject": row.subject,
            "body": row.body,
            "status": row.status,
            "recipients": row.recipients,
            "sent_count": row.sent_count,
        }
