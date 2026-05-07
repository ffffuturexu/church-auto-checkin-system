from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["admin"])


@router.get("/admin", response_class=FileResponse)
def admin_page() -> FileResponse:
    static_file = Path(__file__).resolve().parents[1] / "static" / "admin.html"
    return FileResponse(static_file)
