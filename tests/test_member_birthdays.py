from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import database
from app.models.models import Base, Member
from app.routers import care, members


def test_partial_birthday_age_updates_and_explicit_clears(api_context, monkeypatch):
    client = api_context["client"]
    monkeypatch.setattr(members, "today_local", lambda: date(2026, 3, 1))
    result = client.post("/members", json={
        "name": "Partial", "birthday_month": 2, "birthday_day": 29, "age_at_record": 20,
    })
    assert result.status_code == 200
    member = result.json()
    assert (member["birthday"], member["age"], member["age_recorded_on"]) == (None, None, "2026-03-01")
    member_id = member["id"]

    updated = client.put(f"/members/{member_id}", json={"name": "Renamed", "birthday": None}).json()
    assert (updated["birthday_month"], updated["birthday_day"], updated["age_at_record"]) == (2, 29, 20)
    assert client.put(f"/members/{member_id}", json={"birthday_day": None}).status_code == 422
    assert client.get(f"/members/{member_id}").json()["birthday_day"] == 29
    monkeypatch.setattr(members, "today_local", lambda: date(2026, 3, 2))
    assert client.put(f"/members/{member_id}", json={"age_at_record": 20}).json()["age_recorded_on"] == "2026-03-01"
    assert client.put(f"/members/{member_id}", json={"age_at_record": 21}).json()["age_recorded_on"] == "2026-03-02"
    assert client.put(f"/members/{member_id}", json={"birthday": "2000-01-01"}).status_code == 422
    result = client.put(f"/members/{member_id}", json={
        "birthday": "2000-01-01", "birthday_month": None, "birthday_day": None, "age_at_record": None,
    })
    assert result.status_code == 200
    assert result.json()["age_recorded_on"] is None
    assert result.json()["birthday"] == "2000-01-01"
    assert client.put(f"/members/{member_id}", json={"birthday": None}).json()["birthday"] is None


@pytest.mark.parametrize("fields", [
    {"birthday_month": 2}, {"birthday_day": 2}, {"birthday_month": 2, "birthday_day": 30},
    {"birthday_month": 4, "birthday_day": 31}, {"birthday_month": 13, "birthday_day": 1},
    {"birthday_month": True, "birthday_day": 1}, {"age_at_record": -1}, {"age_at_record": 131},
    {"age_at_record": 2.5}, {"birthday": "2999-01-01"},
    {"birthday": "2000-01-01", "age_at_record": 0},
    {"birthday": "2000-01-01", "birthday_month": 1, "birthday_day": 1},
])
def test_invalid_birthday_fields(api_context, fields):
    assert api_context["client"].post("/members", json={"name": "Invalid", **fields}).status_code == 422


def test_create_idempotency(api_context):
    client = api_context["client"]
    member_id = str(uuid4())
    payload = {"id": member_id, "name": " Test ", "birthday_month": 2, "birthday_day": 29, "age_at_record": 0}
    first = client.post("/members", json=payload)
    second = client.post("/members", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["id"] == member_id
    assert client.post("/members", json={**payload, "age_at_record": 1}).status_code == 409
    assert client.post("/members", json={**payload, "name": "Different"}).status_code == 409
    assert client.post("/members", json={"name": "Test"}).json()["id"] != member_id
    with api_context["session_factory"]() as db:
        assert len(db.scalars(select(Member)).all()) == 2


@pytest.mark.parametrize(("existing_name", "expected_status"), [("Concurrent", 200), ("Different", 409)])
def test_create_idempotency_after_concurrent_insert(api_context, monkeypatch, existing_name, expected_status):
    client = api_context["client"]
    factory = api_context["session_factory"]
    original_commit = Session.commit
    member_id = uuid4()
    attempted = False

    def competing_insert(session):
        nonlocal attempted
        if not attempted and any(isinstance(item, Member) and item.id == member_id for item in session.new):
            attempted = True
            with factory() as other:
                other.add(Member(id=member_id, name=existing_name, status=True))
                original_commit(other)
            raise IntegrityError("insert", {}, Exception("duplicate"))
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", competing_insert)
    response = client.post("/members", json={"id": str(member_id), "name": "Concurrent"})
    assert response.status_code == expected_status
    if expected_status == 200:
        assert response.json()["id"] == str(member_id)
    assert attempted
    with factory() as db:
        assert len(db.scalars(select(Member)).all()) == 1


@pytest.fixture
def leap_day_members(api_context):
    client = api_context["client"]
    for data in [
        {"name": "Partial", "birthday_month": 2, "birthday_day": 29, "age_at_record": 5},
        {"name": "Full", "birthday": "2000-02-29"},
        {"name": "Inactive", "birthday_month": 2, "birthday_day": 29, "status": False},
    ]:
        assert client.post("/members", json=data).status_code == 200
    return api_context


def test_care_birthdays_with_partial_leap_day(leap_day_members, monkeypatch):
    client = leap_day_members["client"]
    monkeypatch.setattr(care, "today_local", lambda: date(2025, 2, 27))
    items = client.get("/care/birthdays", params={"days": 1}).json()["items"]
    by_name = {item["name"]: item for item in items}
    assert set(by_name) == {"Partial", "Full"}
    assert by_name["Partial"]["birthday"] is None
    assert by_name["Partial"]["birthday_month"] == 2
    assert by_name["Partial"]["next_birthday"] == "2025-02-28"
    assert by_name["Partial"]["turning_age"] is None
    assert by_name["Full"]["turning_age"] == 25
    monkeypatch.setattr(care, "today_local", lambda: date(2028, 2, 28))
    assert {item["next_birthday"] for item in client.get("/care/birthdays", params={"days": 1}).json()["items"]} == {"2028-02-29"}


def test_reminders_with_partial_leap_day(leap_day_members, monkeypatch):
    reminders = pytest.importorskip("scripts.send_birthday_reminders")
    monkeypatch.setattr(reminders, "today_local", lambda: date(2025, 2, 27))
    monkeypatch.setattr(reminders, "SessionLocal", leap_day_members["session_factory"])
    rows = reminders._load_members_with_target_birthday(1)
    assert len(rows) == 2
    partial = next(row for row in rows if row["name"] == "Partial")
    assert partial["birthday"] == "--02-29"
    assert partial["turning_age"] is None
    assert len(reminders._load_members_within_days(1)) == 2
    assert "将满: 未知" in reminders._build_email(1, rows, "a@example.org", ["b@example.org"]).get_content()
    monkeypatch.setattr(reminders, "today_local", lambda: date(2028, 2, 28))
    assert len(reminders._load_members_with_target_birthday(1)) == 2


def test_sqlite_migration_preserves_existing_data(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for column in ("birthday_month", "birthday_day", "age_at_record", "age_recorded_on"):
            conn.execute(text(f"ALTER TABLE members DROP COLUMN {column}"))
        conn.execute(text("ALTER TABLE members ADD COLUMN age INTEGER"))
        conn.execute(text("INSERT INTO members (id, name, birthday, age, status, has_photo) "
                          "VALUES ('old-id', 'Existing', '2000-02-29', 10, 1, 0)"))
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "DATABASE_URL", "sqlite:///:memory:")
    database._apply_sqlite_migrations()
    database._apply_sqlite_migrations()
    with engine.connect() as conn:
        row = conn.execute(text("SELECT name, birthday, birthday_month, birthday_day, age_at_record, age_recorded_on FROM members")).one()
        assert tuple(row) == ("Existing", "2000-02-29", None, None, None, None)
        assert "age" not in {row[1] for row in conn.execute(text("PRAGMA table_info(members)"))}
    engine.dispose()
