from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["reception"])


@router.get("/reception", response_class=FileResponse)
def reception_page() -> FileResponse:
    static_file = Path(__file__).resolve().parents[1] / "static" / "reception.html"
    return FileResponse(static_file)
