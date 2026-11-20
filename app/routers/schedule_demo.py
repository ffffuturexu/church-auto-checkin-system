from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["schedule-demo"])


@router.get("/schedule-demo", response_class=FileResponse, include_in_schema=False)
def schedule_demo_page() -> FileResponse:
    static_file = Path(__file__).resolve().parents[1] / "static" / "schedule-demo.html"
    return FileResponse(static_file)
