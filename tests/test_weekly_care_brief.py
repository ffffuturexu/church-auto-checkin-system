from __future__ import annotations

import os
import json
from typing import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import SessionLocal, get_db
from app.models.models import AttendanceEvent, AttendanceRecord, CheckInMethod, Member
from app.routers.attendance import router as attendance_router
from app.routers.care import router as care_router
from app.routers.events import router as events_router
from app.routers.face_library import router as face_library_router
from app.routers.members import router as members_router
from app.routers.reception_queue import router as reception_queue_router
from app.services.face_library_service import FaceLibraryService


class FakeCompreFaceClient:
    def __init__(self) -> None:
        self.created_subjects: list[str] = []
        self.added_faces: list[tuple[str, str, str]] = []
        self.deleted_faces: list[str] = []
        self.deleted_subjects: list[str] = []

    def create_subject(self, name: str) -> dict:
        self.created_subjects.append(name)
        return {"status": "ok", "subject": name}

    def add_face_image(self, subject: str, image_path: str) -> dict:
        face_id = f"face-{len(self.added_faces) + 1}"
        self.added_faces.append((subject, image_path, face_id))
        return {"face_id": face_id}

    def delete_face(self, face_id: str) -> dict:
        self.deleted_faces.append(face_id)
        return {"status": "deleted", "face_id": face_id}

    def delete_subject(self, subject: str) -> dict:
        self.deleted_subjects.append(subject)
        return {"status": "deleted", "subject": subject}


class DummyWsManager:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def broadcast_channel_a(self, payload: dict) -> None:
        self.messages.append(payload)


def _create_member(session_factory, name: str, group: str | None, has_photo: bool, status: bool = True) -> str:
    with session_factory() as db:
        row = Member(
            name=name,
            name_chn=None,
            group=group,
            status=status,
            has_photo=has_photo,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return str(row.id)


def _recent_sunday(today: date, offset_weeks: int = 0) -> date:
    days_since_sunday = (today.weekday() + 1) % 7
    latest = today - timedelta(days=days_since_sunday)
    return latest - timedelta(days=offset_weeks * 7)


def _create_event_and_checkin(session_factory, member_id: str, event_date: date, event_name: str) -> None:
    with session_factory() as db:
        event = AttendanceEvent(event_name=event_name, event_date=event_date, is_archived=False)
        db.add(event)
        db.commit()
        db.refresh(event)

        record = AttendanceRecord(
            event_id=event.id,
            member_id=UUID(member_id),
            check_in_time=datetime.combine(event_date, datetime.min.time()),
            method=CheckInMethod.AUTO_FACE,
        )
        db.add(record)
        db.commit()


@pytest.fixture()
def real_api_context(tmp_path: Path):
    app = FastAPI(title="real-db-test-app")
    app.include_router(members_router)
    app.include_router(events_router)
    app.include_router(attendance_router)
    app.include_router(care_router)
    app.include_router(face_library_router)
    app.include_router(reception_queue_router)

    fake_client = FakeCompreFaceClient()
    app.state.face_library_service = FaceLibraryService(fake_client, str(tmp_path / "faces"))
    app.state.ws_manager = DummyWsManager()

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        yield {
            "client": client,
            "session_factory": SessionLocal,
            "fake_client": fake_client,
            "ws_manager": app.state.ws_manager,
        }


def _fetch_json(client, path: str, params: dict[str, object] | None = None) -> dict:
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _build_weekly_brief_payload(attendance_dashboard: dict, care_members: dict) -> dict:
    summary = attendance_dashboard["summary"]
    daily = attendance_dashboard.get("daily", [])
    top_risk_rows = care_members.get("items", [])[:3]

    this_week = daily[-1] if daily else {"checkins_zh": 0, "checkins_it": 0, "event_date": None}
    last_week = daily[-2] if len(daily) >= 2 else {"checkins_zh": 0, "checkins_it": 0, "event_date": None}

    zh_delta = int(this_week.get("checkins_zh", 0)) - int(last_week.get("checkins_zh", 0))
    it_delta = int(this_week.get("checkins_it", 0)) - int(last_week.get("checkins_it", 0))
    total_this_week = int(this_week.get("checkins_zh", 0)) + int(this_week.get("checkins_it", 0))
    total_last_week = int(last_week.get("checkins_zh", 0)) + int(last_week.get("checkins_it", 0))

    overview_lines = [
        f"本周总览：中文场次 {this_week.get('checkins_zh', 0)} 人，较上周 {zh_delta:+d}；意语场次 {this_week.get('checkins_it', 0)} 人，较上周 {it_delta:+d}。",
        f"签到总览：本周合计 {total_this_week} 人，较上周 {total_this_week - total_last_week:+d}。年内平均每场中文 {summary['avg_this_year_zh']} 人，意语 {summary['avg_this_year_it']} 人。",
        f"结构性观察：当前 active 成员 {summary['active_members']} 人，待处理异常抓拍 {summary['pending_unknown_cases']} 个。",
    ]

    change_lines = []
    for item in top_risk_rows:
        last_checkin = item.get("last_checkin_date") or "未知"
        days_since = item.get("days_since_last_checkin")
        change_lines.append(
            f"{item['name']}：最近签到 {last_checkin}，距今 {days_since if days_since is not None else '未知'} 天，风险 {item['risk_level']} / {item['risk_score']}。"
        )

    if not change_lines:
        change_lines.append("本周暂无需要重点标记的新近变化。")

    return {
        "overview": overview_lines,
        "changes": change_lines,
        "top_risk": [
            {
                "name": item["name"],
                "group": item.get("group"),
                "risk_level": item["risk_level"],
                "risk_score": item["risk_score"],
            }
            for item in top_risk_rows
        ],
    }


def _render_final_brief(payload: dict, session_factory) -> str:
    # Section 1: 本周聚会概览
    summary = payload.get("summary", {})
    daily = payload.get("daily", [])
    this_week = daily[-1] if daily else {"checkins_zh": 0, "checkins_it": 0, "event_date": None}

    zh = int(this_week.get("checkins_zh", 0))
    it = int(this_week.get("checkins_it", 0))
    total = zh + it

    overview_lines = [
        f"本周举办的聚会：中文 / 意语。",
        f"本周实到人数：中文 {zh} 人，意语 {it} 人，合计 {total} 人。",
    ]

    # Section 2: 分组出席情况 — compute per-group counts and rates
    group_rows = _compute_group_attendance(session_factory, this_week.get("event_date"))

    # Section 3: 趋势 — last 4 weeks with dates, separated by language and total
    recent_points = daily[-4:] if len(daily) >= 4 else daily
    # pad with leading zeros if fewer than 4
    if len(recent_points) < 4:
        recent_points = ([{"event_date": None, "checkins": 0, "checkins_zh": 0, "checkins_it": 0}] * (4 - len(recent_points))) + recent_points

    # build display rows with date and counts
    trend_lines = []
    for pt in recent_points:
        d = pt.get("event_date") or "-"
        zh_count = int(pt.get("checkins_zh", 0))
        it_count = int(pt.get("checkins_it", 0))
        tot = int(pt.get("checkins", zh_count + it_count))
        trend_lines.append({"date": d, "zh": zh_count, "it": it_count, "total": tot})

    # compute week-over-week deltas for zh/it/total
    cur = trend_lines[-1]
    prev = trend_lines[-2]
    delta_zh = cur["zh"] - prev["zh"]
    delta_it = cur["it"] - prev["it"]
    delta_tot = cur["total"] - prev["total"]
    pct_zh = round((delta_zh / prev["zh"] * 100), 1) if prev["zh"] else None
    pct_it = round((delta_it / prev["it"] * 100), 1) if prev["it"] else None
    pct_tot = round((delta_tot / prev["total"] * 100), 1) if prev["total"] else None

    # Render template in Chinese
    parts: list[str] = []
    parts.append("# 教会关怀秘书周报")
    parts.append("\n**第一部分：本周聚会概览**")
    parts.extend(overview_lines)

    parts.append("\n**第二部分：分组出席情况（仅统计中文场次）**")
    if group_rows:
        for row in group_rows:
            rate_text = (
                f"（出席率 {row['attendance_rate']:.1f}% ，{row['present']}/{row['group_size']})"
                if row.get("group_size") and row.get("attendance_rate") is not None
                else f"（{row['present']} 人）"
            )
            parts.append(f"- {row['group']}: {row['present']} 人 {rate_text}")
    else:
        parts.append("- 本周暂无分组出席数据。")

    parts.append("\n**第三部分：趋势**")
    parts.append("- 过去 4 周出席人数（含日期，格式：日期 — 中文 / 意语 / 总计）：")
    for row in trend_lines:
        parts.append(f"  - {row['date']}: 中文 {row['zh']} 人，意语 {row['it']} 人，合计 {row['total']} 人")

    # week-over-week summary
    zh_pct_text = f"（{pct_zh:+.1f}%）" if pct_zh is not None else ""
    it_pct_text = f"（{pct_it:+.1f}%）" if pct_it is not None else ""
    tot_pct_text = f"（{pct_tot:+.1f}%）" if pct_tot is not None else ""

    parts.append(f"- 本周中文 {cur['zh']} 人，较上周 {delta_zh:+d} 人{zh_pct_text}。")
    parts.append(f"- 本周意语 {cur['it']} 人，较上周 {delta_it:+d} 人{it_pct_text}。")
    parts.append(f"- 本周合计 {cur['total']} 人，较上周 {delta_tot:+d} 人{tot_pct_text}。")

    return "\n".join(parts)


def _compute_group_attendance(session_factory, event_date) -> list[dict]:
    # Exclude these groups from statistics
    excluded = {"路过", "不稳定", "教会小组", "未分组", "待小组", "其他教会", "神学生"}
    with session_factory() as db:
        # attendance counts per group for the given event_date
        from sqlalchemy import select, func, or_
        from app.models.models import AttendanceEvent, AttendanceRecord, Member

        if event_date is None:
            return []

        rows = db.execute(
            select(Member.group, func.count(AttendanceRecord.id))
            .join(AttendanceRecord, AttendanceRecord.member_id == Member.id)
            .join(AttendanceEvent, AttendanceRecord.event_id == AttendanceEvent.id)
            .where(AttendanceEvent.event_date == event_date)
            .group_by(Member.group)
        ).all()

        # Only count Chinese (中文/华语) service checkins for group attendance
        lowered_name = func.lower(AttendanceEvent.event_name)
        zh_filter = or_(
            AttendanceEvent.event_name.like("%中文%"),
            AttendanceEvent.event_name.like("%华语%"),
            lowered_name.like("%chinese%"),
            lowered_name.like("%mandarin%"),
        )

        rows = db.execute(
            select(Member.group, func.count(AttendanceRecord.id))
            .join(AttendanceRecord, AttendanceRecord.member_id == Member.id)
            .join(AttendanceEvent, AttendanceRecord.event_id == AttendanceEvent.id)
            .where(AttendanceEvent.event_date == event_date, zh_filter)
            .group_by(Member.group)
        ).all()
        # member counts per group (active members)
        member_counts = db.execute(
            select(Member.group, func.count(Member.id)).where(Member.status.is_(True)).group_by(Member.group)
        ).all()

        member_map = {row[0] or "未分组": int(row[1] or 0) for row in member_counts}
        attendance_map = {row[0] or "未分组": int(row[1] or 0) for row in rows}

        results: list[dict] = []
        for group, present in attendance_map.items():
            name = group or "未分组"
            if name in excluded:
                continue
            group_size = member_map.get(group, 0)
            attendance_rate = None
            if group_size:
                attendance_rate = round((present / group_size) * 100, 1)
            results.append({"group": name, "present": present, "group_size": group_size, "attendance_rate": attendance_rate})

        # sort by present desc
        results.sort(key=lambda r: r["present"], reverse=True)
        return results





@pytest.mark.integration
def test_weekly_care_brief_data_and_generation(real_api_context, tmp_path: Path):
    client = real_api_context["client"]
    try:
        attendance_dashboard = _fetch_json(client, "/attendance/dashboard", {"days": 14, "top_events_limit": 8})
        care_members = _fetch_json(
            client,
            "/care/members",
            {
                "months_window": 3,
                "only_sunday": True,
                "status_filter": "active",
                "sort_by": "risk_score",
                "sort_dir": "desc",
                "limit": 10,
            },
        )

        assert attendance_dashboard["summary"]["active_members"] > 0
        assert care_members["items"]

        payload = _build_weekly_brief_payload(attendance_dashboard, care_members)
        # attach raw dashboard data for deterministic rendering
        payload["summary"] = attendance_dashboard.get("summary", {})
        payload["daily"] = attendance_dashboard.get("daily", [])
        brief_text = _render_final_brief(payload, real_api_context["session_factory"])

        assert "第一部分：本周聚会概览" in brief_text
        assert "第二部分：分组出席情况" in brief_text
        assert "第三部分：趋势" in brief_text
        # check that numeric totals from dashboard appear
        this_week = attendance_dashboard.get("daily", [])[-1] if attendance_dashboard.get("daily") else {"checkins_zh": 0, "checkins_it": 0}
        for token in (str(this_week.get("checkins_zh", 0)), str(this_week.get("checkins_it", 0)), str(this_week.get("checkins", this_week.get("checkins_zh", 0) + this_week.get("checkins_it", 0)))):
            assert token in brief_text

        output_path = tmp_path / "weekly_care_brief_sample.txt"
        output_path.write_text(
            "WEEKLY_CARE_BRIEF_TEMPLATE\n\n" + brief_text + "\n",
            encoding="utf-8",
        )

        print("\n=== RAW /attendance/dashboard JSON ===\n")
        print(json.dumps(attendance_dashboard, ensure_ascii=False, indent=2, default=str))
        print("\n=== RAW /care/members JSON ===\n")
        print(json.dumps(care_members, ensure_ascii=False, indent=2, default=str))
        print("\n=== STRUCTURED BRIEF PAYLOAD ===\n")
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        print("\n=== WEEKLY CARE BRIEF ===\n")
        print(brief_text)
        assert output_path.exists()
    finally:
        # nothing to stop — deterministic template only
        pass