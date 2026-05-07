from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.time_utils import now_local_naive, parse_timestamp_to_local_naive
from app.models.models import ReceptionFeedEvent, ReceptionFeedState

FEED_STATE_KEY = "global"


def ensure_feed_state(db: Session) -> ReceptionFeedState:
    state = db.get(ReceptionFeedState, FEED_STATE_KEY)
    if state is None:
        state = ReceptionFeedState(name=FEED_STATE_KEY)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def record_feed_event(db: Session, payload: dict, source: str | None = None) -> ReceptionFeedEvent:
    event_type = str(payload.get("event_type") or "").strip() or "unknown"
    timestamp = payload.get("timestamp")
    occurred_at = parse_timestamp_to_local_naive(timestamp) if timestamp else now_local_naive()
    event = ReceptionFeedEvent(
        event_type=event_type,
        occurred_at=occurred_at,
        payload_json=json.dumps(payload, ensure_ascii=True),
        source=source,
    )
    db.add(event)
    return event
