from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class ReceptionFeedEventOut(BaseModel):
    id: UUID
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any]
    source: str | None = None


class ReceptionFeedListResponse(BaseModel):
    total: int
    items: list[ReceptionFeedEventOut]


class ReceptionFeedClearResponse(BaseModel):
    status: str
    cleared_at: datetime
