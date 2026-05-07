from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete

from app.core.database import SessionLocal
from app.models.models import RecognitionLog, ReceptionFeedEvent


@dataclass
class CleanupState:
    running: bool
    retention_days: int
    interval_sec: int
    last_run_at: str | None
    last_deleted_rows: int


class RecognitionLogCleanupService:
    """Periodically remove recognition logs older than retention window."""

    def __init__(self, retention_days: int = 7, interval_sec: int = 3600) -> None:
        self.retention_days = retention_days
        self.interval_sec = interval_sec
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self.last_run_at: datetime | None = None
        self.last_deleted_rows: int = 0

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="recognition-log-cleanup")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def state(self) -> CleanupState:
        return CleanupState(
            running=bool(self._task and not self._task.done()),
            retention_days=self.retention_days,
            interval_sec=self.interval_sec,
            last_run_at=self.last_run_at.isoformat(timespec="seconds") if self.last_run_at else None,
            last_deleted_rows=self.last_deleted_rows,
        )

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            await self.run_once()
            await asyncio.sleep(self.interval_sec)

    async def run_once(self) -> int:
        cutoff = datetime.utcnow() - timedelta(days=self.retention_days)
        with SessionLocal() as db:
            stmt = delete(RecognitionLog).where(RecognitionLog.timestamp < cutoff)
            result = db.execute(stmt)
            db.commit()
            deleted = int(result.rowcount or 0)

        self.last_run_at = datetime.utcnow()
        self.last_deleted_rows = deleted
        return deleted


@dataclass
class FeedCleanupState:
    running: bool
    retention_days: int
    interval_sec: int
    last_run_at: str | None
    last_deleted_rows: int


class ReceptionFeedCleanupService:
    """Periodically remove reception feed events older than retention window."""

    def __init__(self, retention_days: int = 14, interval_sec: int = 3600) -> None:
        self.retention_days = retention_days
        self.interval_sec = interval_sec
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self.last_run_at: datetime | None = None
        self.last_deleted_rows: int = 0

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="reception-feed-cleanup")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def state(self) -> FeedCleanupState:
        return FeedCleanupState(
            running=bool(self._task and not self._task.done()),
            retention_days=self.retention_days,
            interval_sec=self.interval_sec,
            last_run_at=self.last_run_at.isoformat(timespec="seconds") if self.last_run_at else None,
            last_deleted_rows=self.last_deleted_rows,
        )

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            await self.run_once()
            await asyncio.sleep(self.interval_sec)

    async def run_once(self) -> int:
        cutoff = datetime.utcnow() - timedelta(days=self.retention_days)
        with SessionLocal() as db:
            stmt = delete(ReceptionFeedEvent).where(ReceptionFeedEvent.occurred_at < cutoff)
            result = db.execute(stmt)
            db.commit()
            deleted = int(result.rowcount or 0)

        self.last_run_at = datetime.utcnow()
        self.last_deleted_rows = deleted
        return deleted
