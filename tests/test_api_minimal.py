from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from requests import HTTPError, Response

import app.routers.attendance as attendance_router_module
from app.models.models import AttendanceEvent, UnknownCaseStatus, UnknownFaceCase


def _create_member(client, name: str = "Test Member") -> dict:
    resp = client.post(
        "/members",
        json={"name": name, "name_chn": "\u6d4b\u8bd5", "group": "A", "status": True},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["has_photo"] is False
    return payload


def _create_unknown_case(
    session_factory,
    reason: str = "unknown",
    timestamp: datetime | None = None,
) -> str:
    with session_factory() as db:
        row = UnknownFaceCase(
            timestamp=timestamp or datetime(2026, 4, 5, 10, 0, 0),
            reason=reason,
            image_base64="ZmFrZS1pbWFnZQ==",
            status=UnknownCaseStatus.PENDING,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return str(row.id)


def _get_event_name(session_factory, event_id: str) -> str:
    with session_factory() as db:
        row = db.get(AttendanceEvent, UUID(event_id))
        assert row is not None
        return row.event_name


def test_face_library_crud_sync(api_context):
    client = api_context["client"]
    fake_client = api_context["fake_client"]

    member = _create_member(client, name="Photo Owner")
    member_id = member["id"]

    upload = client.post(
        f"/face-library/members/{member_id}/photos",
        files={"file": ("sample.jpg", b"face-bytes-1", "image/jpeg")},
    )
    assert upload.status_code == 200
    photo = upload.json()["photo"]
    photo_id = photo["id"]
    assert photo["is_active"] is True
    assert photo["remote_face_id"] == "face-1"

    listing = client.get(f"/face-library/members/{member_id}/photos")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    member_after_upload = client.get(f"/members/{member_id}")
    assert member_after_upload.status_code == 200
    assert member_after_upload.json()["has_photo"] is True

    replaced = client.put(
        f"/face-library/photos/{photo_id}",
        files={"file": ("sample2.jpg", b"face-bytes-2", "image/jpeg")},
    )
    assert replaced.status_code == 200
    replaced_photo = replaced.json()["photo"]
    assert replaced_photo["remote_face_id"] == "face-2"

    deleted = client.delete(f"/face-library/photos/{photo_id}")
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"

    active_listing = client.get(f"/face-library/members/{member_id}/photos")
    assert active_listing.status_code == 200
    assert active_listing.json()["total"] == 0

    member_after_delete = client.get(f"/members/{member_id}")
    assert member_after_delete.status_code == 200
    assert member_after_delete.json()["has_photo"] is False

    assert len(fake_client.created_subjects) >= 1
    assert len(fake_client.added_faces) == 2
    assert fake_client.deleted_faces == ["face-1", "face-2"]


def test_face_library_rebuild_sync(api_context):
    client = api_context["client"]
    fake_client = api_context["fake_client"]

    member = _create_member(client, name="Sync Owner")
    member_id = member["id"]

    upload = client.post(
        f"/face-library/members/{member_id}/photos",
        files={"file": ("sync.jpg", b"sync-bytes", "image/jpeg")},
    )
    assert upload.status_code == 200
    assert upload.json()["photo"]["remote_face_id"] == "face-1"

    rebuilt = client.post(f"/face-library/sync/rebuild?member_id={member_id}&active_only=true")
    assert rebuilt.status_code == 200
    payload = rebuilt.json()
    assert payload["status"] == "ok"
    assert payload["summary"]["processed"] == 1
    assert payload["summary"]["synced"] == 1
    assert payload["summary"]["failed"] == 0

    listing = client.get(f"/face-library/members/{member_id}/photos")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["remote_face_id"] == "face-2"
    assert len(fake_client.added_faces) == 2
    assert len(fake_client.deleted_subjects) >= 1


def test_face_library_upload_returns_compreface_reason_on_400(api_context):
    client = api_context["client"]
    fake_client = api_context["fake_client"]

    member = _create_member(client, name="Upload Error Owner")
    member_id = member["id"]

    def failing_add_face_image(subject: str, image_path: str) -> dict:
        response = Response()
        response.status_code = 400
        response._content = b'{"message":"face is not found","code":28}'
        raise HTTPError("400 Client Error", response=response)

    fake_client.add_face_image = failing_add_face_image  # type: ignore[assignment]

    upload = client.post(
        f"/face-library/members/{member_id}/photos",
        files={"file": ("bad.jpg", b"not-a-face", "image/jpeg")},
    )
    assert upload.status_code == 422
    detail = upload.json()["detail"]
    assert "CompreFace add_face_image failed" in detail
    assert "code=28" in detail

    member_after_upload = client.get(f"/members/{member_id}")
    assert member_after_upload.status_code == 200
    assert member_after_upload.json()["has_photo"] is False


def test_reception_queue_ignore_and_resolve(api_context):
    client = api_context["client"]
    session_factory = api_context["session_factory"]

    member = _create_member(client, name="Queue Member")
    member_id = member["id"]

    ignored_case_id = _create_unknown_case(session_factory, reason="no_match")

    pending = client.get("/reception/queue/unknown?status=pending")
    assert pending.status_code == 200
    assert pending.json()["total"] >= 1

    ignored = client.post(f"/reception/queue/unknown/{ignored_case_id}/ignore", json={"note": "skip"})
    assert ignored.status_code == 200
    assert ignored.json()["status"] == "ignored"
    assert ignored.json()["case"]["status"] == "ignored"

    resolve_case_id = _create_unknown_case(session_factory, reason="manual_bind")
    resolved = client.post(
        f"/reception/queue/unknown/{resolve_case_id}/resolve",
        json={"member_id": member_id},
    )
    assert resolved.status_code == 200
    resolved_payload = resolved.json()
    assert resolved_payload["status"] == "resolved"
    assert resolved_payload["case"]["attendance_record_id"] is not None
    event_id = resolved_payload["case"]["event_id"]

    duplicate_case_id = _create_unknown_case(session_factory, reason="manual_bind_duplicate")
    duplicate = client.post(
        f"/reception/queue/unknown/{duplicate_case_id}/resolve",
        json={"member_id": member_id, "event_id": event_id},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["message"] == "member already checked in for selected event"

    history = client.get(f"/attendance/history?event_id={event_id}&member_id={member_id}&limit=10")
    assert history.status_code == 200
    assert history.json()["total"] == 1

    _create_unknown_case(session_factory, reason="to_clear_1")
    _create_unknown_case(session_factory, reason="to_clear_2")

    cleared = client.post("/reception/queue/unknown/clear", json={"note": "bulk-clear"})
    assert cleared.status_code == 200
    assert cleared.json()["status"] == "ok"
    assert cleared.json()["cleared"] == 2

    pending_after_clear = client.get("/reception/queue/unknown?status=pending")
    assert pending_after_clear.status_code == 200
    assert pending_after_clear.json()["total"] == 0


def test_events_crud_archive(api_context):
    client = api_context["client"]

    created = client.post(
        "/events",
        json={"event_name": "Sunday Service", "event_date": "2025-01-05"},
    )
    assert created.status_code == 200
    event = created.json()
    event_id = event["id"]
    assert event["is_archived"] is False

    updated = client.put(
        f"/events/{event_id}",
        json={"event_name": "Sunday Service AM", "event_date": "2025-01-05"},
    )
    assert updated.status_code == 200
    assert updated.json()["event_name"] == "Sunday Service AM"

    archived = client.post(f"/events/{event_id}/archive", json={"is_archived": True})
    assert archived.status_code == 200
    assert archived.json()["is_archived"] is True

    active_only = client.get("/events")
    assert active_only.status_code == 200
    assert active_only.json()["total"] == 0

    include_archived = client.get("/events?include_archived=true")
    assert include_archived.status_code == 200
    assert include_archived.json()["total"] == 1


def test_manual_checkin_duplicate_returns_existing_record(api_context):
    client = api_context["client"]

    member_resp = client.post(
        "/members",
        json={"name": "Manual Checkin Member", "status": True},
    )
    assert member_resp.status_code == 200
    member_id = member_resp.json()["id"]

    event_resp = client.post(
        "/events",
        json={"event_name": "Sunday Service", "event_date": "2026-04-05"},
    )
    assert event_resp.status_code == 200
    event_id = event_resp.json()["id"]

    first = client.post(
        "/attendance/manual-checkin",
        json={"member_id": member_id, "event_id": event_id},
    )
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["status"] == "ok"

    second = client.post(
        "/attendance/manual-checkin",
        json={"member_id": member_id, "event_id": event_id},
    )
    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["status"] == "duplicate"
    assert second_payload["record"]["id"] == first_payload["record"]["id"]

    history = client.get(f"/attendance/history?event_id={event_id}&member_id={member_id}&limit=10")
    assert history.status_code == 200
    assert history.json()["total"] == 1


def test_manual_checkin_non_sunday_is_ignored_without_creating_event(api_context):
    client = api_context["client"]

    member_resp = client.post(
        "/members",
        json={"name": "Manual Non Sunday Member", "status": True},
    )
    assert member_resp.status_code == 200
    member_id = member_resp.json()["id"]

    non_sunday_resp = client.post(
        "/attendance/manual-checkin",
        json={"member_id": member_id, "event_date": "2026-04-06"},
    )
    assert non_sunday_resp.status_code == 409
    assert "only allowed on Sunday" in non_sunday_resp.json()["detail"]

    events = client.get("/events?include_archived=true&limit=200")
    assert events.status_code == 200
    dates = {item["event_date"] for item in events.json()["items"]}
    assert "2026-04-06" not in dates


def test_current_service_non_sunday(api_context, monkeypatch):
    client = api_context["client"]

    monkeypatch.setattr(
        attendance_router_module,
        "now_local_naive",
        lambda: datetime(2026, 4, 6, 10, 0, 0),
    )

    resp = client.get("/attendance/current-service")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["is_sunday"] is False
    assert payload["display_text"] == "非主日"
    assert payload["event_name"] is None


def test_current_service_sunday_after_15_is_it(api_context, monkeypatch):
    client = api_context["client"]

    monkeypatch.setattr(
        attendance_router_module,
        "now_local_naive",
        lambda: datetime(2026, 4, 12, 16, 10, 0),
    )

    resp = client.get("/attendance/current-service")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["is_sunday"] is True
    assert payload["service_language"] == "it"
    assert payload["service_title"] == "主日崇拜（意语）"
    assert payload["display_text"] == "2026-04-12-主日崇拜（意语）"
    assert payload["event_name"] == "主日崇拜（意语） 2026-04-12"


def test_current_service_first_sunday_after_15_stays_zh(api_context, monkeypatch):
    client = api_context["client"]

    monkeypatch.setattr(
        attendance_router_module,
        "now_local_naive",
        lambda: datetime(2026, 4, 5, 16, 10, 0),
    )

    resp = client.get("/attendance/current-service")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["is_sunday"] is True
    assert payload["service_language"] == "zh"
    assert payload["service_title"] == "主日崇拜（中文）"
    assert payload["display_text"] == "2026-04-05-主日崇拜（中文）"
    assert payload["event_name"] == "主日崇拜（中文） 2026-04-05"


def test_manual_checkin_service_language_override_it(api_context):
    client = api_context["client"]
    session_factory = api_context["session_factory"]

    member_resp = client.post(
        "/members",
        json={"name": "Manual Language IT", "status": True},
    )
    assert member_resp.status_code == 200
    member_id = member_resp.json()["id"]

    manual_resp = client.post(
        "/attendance/manual-checkin",
        json={
            "member_id": member_id,
            "event_date": "2026-04-12",
            "service_language": "it",
        },
    )
    assert manual_resp.status_code == 200
    event_id = manual_resp.json()["record"]["event_id"]

    event_name = _get_event_name(session_factory, event_id)
    assert event_name == "主日崇拜（意语） 2026-04-12"


def test_manual_checkin_first_sunday_ignores_it_override(api_context):
    client = api_context["client"]
    session_factory = api_context["session_factory"]

    member_resp = client.post(
        "/members",
        json={"name": "Manual Language First Sunday", "status": True},
    )
    assert member_resp.status_code == 200
    member_id = member_resp.json()["id"]

    manual_resp = client.post(
        "/attendance/manual-checkin",
        json={
            "member_id": member_id,
            "event_date": "2026-04-05",
            "service_language": "it",
        },
    )
    assert manual_resp.status_code == 200
    event_id = manual_resp.json()["record"]["event_id"]

    event_name = _get_event_name(session_factory, event_id)
    assert event_name == "主日崇拜（中文） 2026-04-05"


def test_reception_queue_resolve_non_sunday_is_ignored_without_creating_event(api_context):
    client = api_context["client"]
    session_factory = api_context["session_factory"]

    member = _create_member(client, name="Queue Non Sunday Member")
    member_id = member["id"]

    case_id = _create_unknown_case(
        session_factory,
        reason="manual_bind_non_sunday",
        timestamp=datetime(2026, 4, 6, 10, 0, 0),
    )

    resolved = client.post(
        f"/reception/queue/unknown/{case_id}/resolve",
        json={"member_id": member_id},
    )
    assert resolved.status_code == 200
    payload = resolved.json()
    assert payload["status"] == "ignored"
    assert payload["case"]["status"] == "ignored"
    assert payload["case"]["attendance_record_id"] is None
    assert "manual check-in ignored: non_sunday" in (payload.get("message") or "")

    events = client.get("/events?include_archived=true&limit=200")
    assert events.status_code == 200
    dates = {item["event_date"] for item in events.json()["items"]}
    assert "2026-04-06" not in dates


def test_reception_queue_resolve_auto_split_by_case_time(api_context):
    client = api_context["client"]
    session_factory = api_context["session_factory"]

    member = _create_member(client, name="Queue Split Member")
    member_id = member["id"]

    case_id = _create_unknown_case(
        session_factory,
        reason="manual_bind_split",
        timestamp=datetime(2026, 4, 12, 16, 10, 0),
    )

    resolved = client.post(
        f"/reception/queue/unknown/{case_id}/resolve",
        json={"member_id": member_id},
    )
    assert resolved.status_code == 200
    payload = resolved.json()
    assert payload["status"] == "resolved"

    event_id = payload["case"]["event_id"]
    event_name = _get_event_name(session_factory, event_id)
    assert event_name == "主日崇拜（意语） 2026-04-12"


def test_reception_queue_resolve_first_sunday_after_15_stays_zh(api_context):
    client = api_context["client"]
    session_factory = api_context["session_factory"]

    member = _create_member(client, name="Queue First Sunday Member")
    member_id = member["id"]

    case_id = _create_unknown_case(
        session_factory,
        reason="manual_bind_first_sunday",
        timestamp=datetime(2026, 4, 5, 16, 10, 0),
    )

    resolved = client.post(
        f"/reception/queue/unknown/{case_id}/resolve",
        json={"member_id": member_id},
    )
    assert resolved.status_code == 200
    payload = resolved.json()
    assert payload["status"] == "resolved"

    event_id = payload["case"]["event_id"]
    event_name = _get_event_name(session_factory, event_id)
    assert event_name == "主日崇拜（中文） 2026-04-05"


def test_reception_queue_non_sunday_ignore_auto_ignores_same_day_same_primary(api_context):
    client = api_context["client"]
    session_factory = api_context["session_factory"]

    member = _create_member(client, name="Queue Non Sunday Batch Member")
    member_id = member["id"]

    first_case_id = _create_unknown_case(
        session_factory,
        reason="failed_threshold",
        timestamp=datetime(2026, 4, 6, 10, 0, 0),
    )
    second_case_id = _create_unknown_case(
        session_factory,
        reason="failed_threshold",
        timestamp=datetime(2026, 4, 6, 10, 0, 1),
    )

    with session_factory() as db:
        first_case = db.get(UnknownFaceCase, first_case_id)
        second_case = db.get(UnknownFaceCase, second_case_id)
        assert first_case is not None
        assert second_case is not None
        first_case.note = f"best_subject_id={member_id};best_subject_name=Queue Non Sunday Batch Member"
        second_case.note = f"best_subject_id={member_id};best_subject_name=Queue Non Sunday Batch Member"
        db.commit()

    resolved = client.post(
        f"/reception/queue/unknown/{first_case_id}/resolve",
        json={"member_id": member_id},
    )
    assert resolved.status_code == 200
    payload = resolved.json()
    assert payload["status"] == "ignored"
    assert "manual check-in ignored: non_sunday" in (payload.get("message") or "")
    assert "auto_ignored_siblings=1" in (payload.get("message") or "")

    pending = client.get("/reception/queue/unknown?status=pending&limit=100")
    assert pending.status_code == 200
    ids = {item["id"] for item in pending.json()["items"]}
    assert second_case_id not in ids


def test_reception_queue_resolve_auto_ignores_same_day_same_primary(api_context):
    client = api_context["client"]
    session_factory = api_context["session_factory"]

    member = _create_member(client, name="Queue Batch Resolve Member")
    member_id = member["id"]

    first_case_id = _create_unknown_case(
        session_factory,
        reason="failed_threshold",
        timestamp=datetime(2026, 4, 5, 10, 0, 0),
    )
    second_case_id = _create_unknown_case(
        session_factory,
        reason="failed_threshold",
        timestamp=datetime(2026, 4, 5, 10, 0, 1),
    )

    with session_factory() as db:
        first_case = db.get(UnknownFaceCase, first_case_id)
        second_case = db.get(UnknownFaceCase, second_case_id)
        assert first_case is not None
        assert second_case is not None
        first_case.note = f"best_subject_id={member_id};best_subject_name=Queue Batch Resolve Member"
        second_case.note = f"best_subject_id={member_id};best_subject_name=Queue Batch Resolve Member"
        db.commit()

    resolved = client.post(
        f"/reception/queue/unknown/{first_case_id}/resolve",
        json={"member_id": member_id},
    )
    assert resolved.status_code == 200
    payload = resolved.json()
    assert payload["status"] == "resolved"
    assert "auto_ignored_siblings=1" in (payload.get("message") or "")

    pending = client.get("/reception/queue/unknown?status=pending&limit=100")
    assert pending.status_code == 200
    ids = {item["id"] for item in pending.json()["items"]}
    assert second_case_id not in ids


def test_members_birthday_note_and_default_active_filter(api_context):
    client = api_context["client"]

    active_member_resp = client.post(
        "/members",
        json={
            "name": "Member A",
            "name_chn": "\u4f1a\u5458A",
            "group": "G1",
            "birthday": "2000-01-02",
            "note": "note-a",
            "status": True,
        },
    )
    assert active_member_resp.status_code == 200
    active_member = active_member_resp.json()
    birthday = date.fromisoformat("2000-01-02")
    today = date.today()
    expected_age = today.year - birthday.year
    if (today.month, today.day) < (birthday.month, birthday.day):
        expected_age -= 1

    assert active_member["name_chn"] == "\u4f1a\u5458A"
    assert active_member["age"] == expected_age
    assert active_member["birthday"] == "2000-01-02"
    assert active_member["note"] == "note-a"
    assert active_member["has_photo"] is False

    inactive_member_resp = client.post(
        "/members",
        json={"name": "Member B", "status": True},
    )
    assert inactive_member_resp.status_code == 200
    inactive_member = inactive_member_resp.json()

    deactivate_resp = client.delete(f"/members/{inactive_member['id']}")
    assert deactivate_resp.status_code == 200
    assert deactivate_resp.json()["status"] == "deactivated"

    default_list_resp = client.get("/members")
    assert default_list_resp.status_code == 200
    default_items = default_list_resp.json()["items"]
    assert len(default_items) == 1
    assert default_items[0]["id"] == active_member["id"]

    inactive_list_resp = client.get("/members?status_filter=inactive")
    assert inactive_list_resp.status_code == 200
    inactive_items = inactive_list_resp.json()["items"]
    assert len(inactive_items) == 1
    assert inactive_items[0]["id"] == inactive_member["id"]
