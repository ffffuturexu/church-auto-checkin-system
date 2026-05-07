from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, Float, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import CHAR, TypeDecorator


class GUID(TypeDecorator):
    """Cross-database UUID type.

    SQLite stores UUID as CHAR(36), while other databases can still use UUID text safely.
    """

    impl = CHAR(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return str(value)
        return str(uuid.UUID(str(value)))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return uuid.UUID(value)


class Base(DeclarativeBase):
    pass


class CheckInMethod(str, enum.Enum):
    AUTO_FACE = "auto_face"
    MANUAL_RECEPTION = "manual_reception"


class RecognitionStatus(str, enum.Enum):
    SUCCESS = "success"
    FAILED_MARGIN = "failed_margin" # 相似度未达到阈值，但仍有一个或多个候选人，可能需要人工审核。
    FAILED_THRESHOLD = "failed_threshold" # 相似度未达到阈值，且没有候选人，直接识别失败。
    UNKNOWN = "unknown"


class UnknownCaseStatus(str, enum.Enum):
    PENDING = "pending"
    IGNORED = "ignored"
    RESOLVED = "resolved"


class Member(Base):
    __tablename__ = "members"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name_chn: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    has_photo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    group: Mapped[str | None] = mapped_column(String(120), nullable=True)
    birthday: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    face_photos: Mapped[list[FacePhoto]] = relationship(
        "FacePhoto",
        back_populates="member",
        cascade="all, delete-orphan",
    )
    attendance_records: Mapped[list[AttendanceRecord]] = relationship(
        "AttendanceRecord",
        back_populates="member",
        cascade="all, delete-orphan",
    )


class AttendanceEvent(Base):
    __tablename__ = "attendance_events"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    event_name: Mapped[str] = mapped_column(String(160), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    attendance_records: Mapped[list[AttendanceRecord]] = relationship(
        "AttendanceRecord",
        back_populates="event",
        cascade="all, delete-orphan",
    )


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("attendance_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    check_in_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    method: Mapped[CheckInMethod] = mapped_column(
        Enum(CheckInMethod, native_enum=False, length=32),
        nullable=False,
    )

    event: Mapped[AttendanceEvent] = relationship("AttendanceEvent", back_populates="attendance_records")
    member: Mapped[Member] = relationship("Member", back_populates="attendance_records")


class RecognitionLog(Base):
    __tablename__ = "recognition_logs"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    best_subject_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    best_subject_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    second_subject_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    second_subject_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    second_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[RecognitionStatus] = mapped_column(
        Enum(RecognitionStatus, native_enum=False, length=32),
        nullable=False,
        index=True,
    )


class FacePhoto(Base):
    __tablename__ = "face_photos"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    member_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    local_path: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    remote_face_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    member: Mapped[Member] = relationship("Member", back_populates="face_photos")


class UnknownFaceCase(Base):
    __tablename__ = "unknown_face_cases"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_base64: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[UnknownCaseStatus] = mapped_column(
        Enum(UnknownCaseStatus, native_enum=False, length=32),
        nullable=False,
        default=UnknownCaseStatus.PENDING,
        index=True,
    )
    member_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("members.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("attendance_events.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    attendance_record_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("attendance_records.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReceptionFeedEvent(Base):
    __tablename__ = "reception_feed_events"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )


class ReceptionFeedState(Base):
    __tablename__ = "reception_feed_state"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


Index("ix_attendance_records_event_member", AttendanceRecord.event_id, AttendanceRecord.member_id)
Index("ix_recognition_logs_subject_status", RecognitionLog.best_subject_id, RecognitionLog.status)
