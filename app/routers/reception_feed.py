from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.time_utils import now_local_naive
from app.models.models import ReceptionFeedEvent
from app.schemas.reception_feed import (
    ReceptionFeedClearResponse,
    ReceptionFeedEventOut,
    ReceptionFeedListResponse,
)
from app.services.reception_feed_service import ensure_feed_state

router = APIRouter(tags=["reception-feed"])


@router.get("/reception/feed", response_model=ReceptionFeedListResponse)
def list_reception_feed(
    limit: int = Query(default=120, ge=1, le=1000),
    after: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
) -> ReceptionFeedListResponse:
    state = ensure_feed_state(db)
    stmt: Select = select(ReceptionFeedEvent)
    if state.cleared_at is not None:
        stmt = stmt.where(ReceptionFeedEvent.occurred_at > state.cleared_at)
    if after is not None:
        stmt = stmt.where(ReceptionFeedEvent.occurred_at > after)

    rows = db.execute(
        stmt.order_by(ReceptionFeedEvent.occurred_at.desc()).limit(limit)
    ).scalars().all()

    items: list[ReceptionFeedEventOut] = []
    for row in rows:
        try:
            payload = json.loads(row.payload_json) if row.payload_json else {}
        except json.JSONDecodeError:
            payload = {}
        items.append(
            ReceptionFeedEventOut(
                id=row.id,
                event_type=row.event_type,
                occurred_at=row.occurred_at,
                payload=payload,
                source=row.source,
            )
        )

    return ReceptionFeedListResponse(total=len(items), items=items)


@router.post("/reception/feed/clear", response_model=ReceptionFeedClearResponse)
def clear_reception_feed(db: Session = Depends(get_db)) -> ReceptionFeedClearResponse:
    state = ensure_feed_state(db)
    state.cleared_at = now_local_naive()
    db.commit()
    db.refresh(state)
    return ReceptionFeedClearResponse(status="ok", cleared_at=state.cleared_at)
