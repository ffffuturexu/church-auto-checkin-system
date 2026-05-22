from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import UUID

from app.models.models import AttendanceEvent, AttendanceRecord, CheckInMethod, Member


def _create_member(session_factory, name: str, group: str | None, has_photo: bool = True, status: bool = True) -> str:
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


def _create_event_and_checkin(session_factory, member_id: str, event_date: date, event_name: str = "Sunday Service") -> None:
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


def _recent_sunday(today: date, offset_weeks: int = 0) -> date:
    days_since_sunday = (today.weekday() + 1) % 7
    latest = today - timedelta(days=days_since_sunday)
    return latest - timedelta(days=offset_weeks * 7)


def test_care_members_filter_by_recent_checkins(api_context):
    client = api_context["client"]
    session_factory = api_context["session_factory"]

    today = date.today()
    sunday0 = _recent_sunday(today, 0)
    sunday1 = _recent_sunday(today, 1)

    low_member = _create_member(session_factory, "Low Member", "新人组", has_photo=False, status=True)
    high_member = _create_member(session_factory, "High Member", "成年组", has_photo=True, status=True)

    _create_event_and_checkin(session_factory, low_member, sunday1)
    _create_event_and_checkin(session_factory, high_member, sunday0)
    _create_event_and_checkin(session_factory, high_member, sunday1)

    resp = client.get(
        "/care/members",
        params={
            "months_window": 3,
            "min_checkins": 0,
            "max_checkins": 1,
            "only_sunday": True,
            "status_filter": "active",
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    names = [item["name"] for item in payload["items"]]

    assert "Low Member" in names
    assert "High Member" not in names


def test_care_profile_report_and_export(api_context):
    client = api_context["client"]
    session_factory = api_context["session_factory"]

    today = date.today()
    sunday0 = _recent_sunday(today, 0)
    sunday2 = _recent_sunday(today, 2)

    member_id = _create_member(session_factory, "Profile Member", "新人组", has_photo=True, status=True)
    _create_event_and_checkin(session_factory, member_id, sunday0)
    _create_event_and_checkin(session_factory, member_id, sunday2)

    profile = client.get(
        f"/care/members/{member_id}/profile",
        params={"months_window": 6, "only_sunday": True, "recent_records_limit": 10},
    )
    assert profile.status_code == 200
    profile_payload = profile.json()
    assert profile_payload["member"]["name"] == "Profile Member"
    assert profile_payload["summary"]["recent_checkins"] >= 2
    assert len(profile_payload["recent_records"]) >= 2

    report = client.get("/care/report", params={"months_window": 6, "only_sunday": True, "status_filter": "active"})
    assert report.status_code == 200
    report_payload = report.json()
    assert report_payload["summary"]["total_members"] >= 1
    assert isinstance(report_payload["risk_distribution"], list)

    export_resp = client.get("/care/members/export.csv", params={"months_window": 6, "only_sunday": True})
    assert export_resp.status_code == 200
    assert "text/csv" in export_resp.headers.get("content-type", "")
    assert "member_id,name" in export_resp.text


def test_care_profile_month_window_uses_calendar_month_start(api_context):
    client = api_context["client"]
    session_factory = api_context["session_factory"]

    today = date.today()
    current_month_start = today.replace(day=1)
    previous_month_end = current_month_start - timedelta(days=1)

    member_id = _create_member(session_factory, "Month Boundary Member", "新人组", has_photo=True, status=True)
    _create_event_and_checkin(session_factory, member_id, previous_month_end)
    _create_event_and_checkin(session_factory, member_id, current_month_start)

    profile = client.get(
        f"/care/members/{member_id}/profile",
        params={"months_window": 1, "only_sunday": False, "recent_records_limit": 10},
    )
    assert profile.status_code == 200
    payload = profile.json()

    assert payload["summary"]["recent_checkins"] == 1
    assert payload["monthly_breakdown"] == [{"month": current_month_start.strftime("%Y-%m"), "checkins": 1}]


def test_care_profile_four_month_window_counts_early_february(api_context):
    client = api_context["client"]
    session_factory = api_context["session_factory"]

    today = date.today()
    month_start = today.replace(day=1)
    feb_start = month_start
    while feb_start.month != 2:
        feb_start = (feb_start.replace(day=1) - timedelta(days=1)).replace(day=1)

    member_id = _create_member(session_factory, "Four Month Member", "新人组", has_photo=True, status=True)
    _create_event_and_checkin(session_factory, member_id, feb_start)
    _create_event_and_checkin(session_factory, member_id, feb_start + timedelta(days=7))
    _create_event_and_checkin(session_factory, member_id, month_start)

    profile = client.get(
        f"/care/members/{member_id}/profile",
        params={"months_window": 4, "only_sunday": False, "recent_records_limit": 10},
    )
    assert profile.status_code == 200
    payload = profile.json()

    assert payload["summary"]["recent_checkins"] == 3
    assert payload["monthly_breakdown"][-1] == {"month": feb_start.strftime("%Y-%m"), "checkins": 2}
