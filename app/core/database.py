from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings


def _sqlite_connect_args(url: str) -> dict[str, bool]:
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def _resolve_database_url(raw_url: str) -> str:
    url = make_url(raw_url)
    if not url.drivername.startswith("sqlite"):
        return raw_url

    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return raw_url

    db_path = Path(database)
    if db_path.is_absolute():
        return raw_url

    repo_root = Path(__file__).resolve().parents[2]
    resolved = (repo_root / db_path).resolve()
    return str(url.set(database=str(resolved)))


DATABASE_URL = _resolve_database_url(settings.database_url)


engine = create_engine(
    DATABASE_URL,
    connect_args=_sqlite_connect_args(DATABASE_URL),
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    class_=Session,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app.models.models import Base

    Base.metadata.create_all(bind=engine)
    _apply_sqlite_migrations()


def _apply_sqlite_migrations() -> None:
    if not DATABASE_URL.startswith("sqlite"):
        return

    with engine.begin() as conn:
        columns = conn.execute(text("PRAGMA table_info(attendance_events)")).fetchall()
        column_names = {str(row[1]) for row in columns}
        if "is_archived" not in column_names:
            conn.execute(
                text("ALTER TABLE attendance_events ADD COLUMN is_archived BOOLEAN NOT NULL DEFAULT 0")
            )

        member_columns = conn.execute(text("PRAGMA table_info(members)")).fetchall()
        member_column_names = {str(row[1]) for row in member_columns}
        if "birthday" not in member_column_names:
            conn.execute(text("ALTER TABLE members ADD COLUMN birthday DATE"))
        if "note" not in member_column_names:
            conn.execute(text("ALTER TABLE members ADD COLUMN note VARCHAR(500)"))
        if "name_chn" not in member_column_names:
            conn.execute(text("ALTER TABLE members ADD COLUMN name_chn VARCHAR(120)"))
        if "has_photo" not in member_column_names:
            conn.execute(text("ALTER TABLE members ADD COLUMN has_photo BOOLEAN NOT NULL DEFAULT 0"))
        if "gender" not in member_column_names:
            conn.execute(text("ALTER TABLE members ADD COLUMN gender VARCHAR(16)"))
        if "age" in member_column_names:
            conn.execute(text("ALTER TABLE members DROP COLUMN age"))
        if "phone" in member_column_names:
            conn.execute(text("ALTER TABLE members DROP COLUMN phone"))

        _migrate_face_photos_to_member_fk(conn)
        _sync_member_has_photo(conn)
        _migrate_recognition_logs_fields(conn)


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name = :table_name"),
        {"table_name": table_name},
    ).fetchone()
    return row is not None


def _migrate_face_photos_to_member_fk(conn) -> None:
    if not _table_exists(conn, "face_photos"):
        return

    photo_columns = conn.execute(text("PRAGMA table_info(face_photos)")).fetchall()
    photo_column_names = {str(row[1]) for row in photo_columns}

    if "member_id" not in photo_column_names:
        conn.execute(text("ALTER TABLE face_photos ADD COLUMN member_id CHAR(36)"))
        photo_column_names.add("member_id")

    has_profile_column = "face_profile_id" in photo_column_names
    has_profile_table = _table_exists(conn, "face_profiles")

    if has_profile_column and has_profile_table:
        conn.execute(
            text(
                """
                UPDATE face_photos
                SET member_id = (
                    SELECT fp.member_id
                    FROM face_profiles AS fp
                    WHERE fp.id = face_photos.face_profile_id
                    LIMIT 1
                )
                WHERE member_id IS NULL
                """
            )
        )

    if has_profile_column:
        conn.execute(text("UPDATE face_photos SET member_id = face_profile_id WHERE member_id IS NULL"))

    if has_profile_column:
        _rebuild_face_photos_table(conn)

    _ensure_face_photos_indexes(conn)

    if _table_exists(conn, "face_profiles"):
        conn.execute(text("DROP TABLE face_profiles"))


def _rebuild_face_photos_table(conn) -> None:
    conn.execute(text("DROP TABLE IF EXISTS face_photos_new"))
    conn.execute(
        text(
            """
            CREATE TABLE face_photos_new (
                id CHAR(36) NOT NULL PRIMARY KEY,
                member_id CHAR(36) NOT NULL,
                local_path VARCHAR(500) NOT NULL UNIQUE,
                original_filename VARCHAR(255) NOT NULL,
                mime_type VARCHAR(80),
                remote_face_id VARCHAR(80),
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(member_id) REFERENCES members (id) ON DELETE CASCADE
            )
            """
        )
    )

    conn.execute(
        text(
            """
            INSERT INTO face_photos_new (
                id,
                member_id,
                local_path,
                original_filename,
                mime_type,
                remote_face_id,
                is_active,
                created_at
            )
            SELECT
                id,
                COALESCE(member_id, face_profile_id),
                local_path,
                original_filename,
                mime_type,
                remote_face_id,
                is_active,
                created_at
            FROM face_photos
            """
        )
    )

    conn.execute(text("DROP TABLE face_photos"))
    conn.execute(text("ALTER TABLE face_photos_new RENAME TO face_photos"))


def _ensure_face_photos_indexes(conn) -> None:
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_face_photos_member_id ON face_photos (member_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_face_photos_remote_face_id ON face_photos (remote_face_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_face_photos_is_active ON face_photos (is_active)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_face_photos_created_at ON face_photos (created_at)"))


def _sync_member_has_photo(conn) -> None:
    if not _table_exists(conn, "members"):
        return

    member_columns = conn.execute(text("PRAGMA table_info(members)")).fetchall()
    member_column_names = {str(row[1]) for row in member_columns}
    if "has_photo" not in member_column_names:
        return

    if not _table_exists(conn, "face_photos"):
        conn.execute(text("UPDATE members SET has_photo = 0"))
        return

    conn.execute(
        text(
            """
            UPDATE members
            SET has_photo = CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM face_photos fp
                    WHERE fp.member_id = members.id
                      AND fp.is_active = 1
                ) THEN 1
                ELSE 0
            END
            """
        )
    )


def _migrate_recognition_logs_fields(conn) -> None:
    if not _table_exists(conn, "recognition_logs"):
        return

    columns = conn.execute(text("PRAGMA table_info(recognition_logs)")).fetchall()
    column_names = {str(row[1]) for row in columns}

    if "best_subject_name" not in column_names:
        conn.execute(text("ALTER TABLE recognition_logs ADD COLUMN best_subject_name VARCHAR(120)"))
    if "second_subject_name" not in column_names:
        conn.execute(text("ALTER TABLE recognition_logs ADD COLUMN second_subject_name VARCHAR(120)"))
    if "second_similarity" not in column_names:
        conn.execute(text("ALTER TABLE recognition_logs ADD COLUMN second_similarity FLOAT"))

    conn.execute(
        text(
            """
            UPDATE recognition_logs
            SET best_subject_name = COALESCE(
                (SELECT m.name FROM members AS m WHERE m.id = recognition_logs.best_subject_id LIMIT 1),
                CASE
                    WHEN best_subject_id = 'unknown' THEN NULL
                    ELSE best_subject_id
                END
            )
            WHERE best_subject_name IS NULL
            """
        )
    )

    conn.execute(
        text(
            """
            UPDATE recognition_logs
            SET second_subject_name = COALESCE(
                (SELECT m.name FROM members AS m WHERE m.id = recognition_logs.second_subject_id LIMIT 1),
                second_subject_id
            )
            WHERE second_subject_id IS NOT NULL
              AND second_subject_name IS NULL
            """
        )
    )
