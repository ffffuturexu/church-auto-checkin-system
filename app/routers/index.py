from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["index"])


@router.get("/", response_class=FileResponse, include_in_schema=False)
def index_page() -> FileResponse:
    static_file = Path(__file__).resolve().parents[1] / "static" / "index.html"
    return FileResponse(static_file)