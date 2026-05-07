from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.core.config import settings

router = APIRouter(tags=["debug"])
security = HTTPBasic()


def require_debug_credentials(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    username_ok = secrets.compare_digest(credentials.username, settings.debug_basic_user)
    password_ok = secrets.compare_digest(credentials.password, settings.debug_basic_password)
    if username_ok and password_ok:
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Debug access requires technical staff credentials.",
        headers={"WWW-Authenticate": "Basic"},
    )


@router.get("/debug", response_class=FileResponse)
def debug_page(_: None = Depends(require_debug_credentials)) -> FileResponse:
    static_file = Path(__file__).resolve().parents[1] / "static" / "debug.html"
    return FileResponse(static_file)
