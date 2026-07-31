"""Academic activities: rounds, meetings, conferences and their attendance."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Date, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SoftDeleteMixin, SyncMixin, TimestampMixin
from app.models.enums import (
    AcademicActivityKind,
    ApprovalStatus,
    ParticipantRole,
)

if TYPE_CHECKING:
    from app.models.duty import AttendanceRecord
    from app.models.identity import User


class AcademicActivity(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    """A scheduled academic event.

    Attendance percentages required by the colleges are computed by comparing
    ``ActivityParticipant`` rows against the activities a trainee was *expected* to
    attend (derived from their org unit and rotation) — see
    ``app.services.requirements.measure_academic_attendance_pct``.
    """

    __tablename__ = "academic_activities"
    __table_args__ = (
        Index("ix_academic_activities_org_time", "org_unit_id", "scheduled_at"),
        Index("ix_academic_activities_kind", "tenant_id", "kind"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_unit_id: Mapped[str] = mapped_column(
        ForeignKey("org_units.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(
        String(48), default=AcademicActivityKind.GRAND_ROUND, nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    abstract: Mapped[str | None] = mapped_column(Text, default=None)
    scheduled_at: Mapped[datetime] = mapped_column(nullable=False, index=True)
    scheduled_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    duration_minutes: Mapped[int] = mapped_column(default=60, nullable=False)
    venue: Mapped[str | None] = mapped_column(String(200), default=None)
    is_virtual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    meeting_url: Mapped[str | None] = mapped_column(String(512), default=None)

    presenter_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )
    moderator_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    external_presenter: Mapped[str | None] = mapped_column(String(200), default=None)

    #: Training levels expected to attend, e.g. ["registrar", "senior_registrar"].
    expected_levels: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cme_credits: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Rolling series identifier, e.g. all Thursday grand rounds share a series.
    series_code: Mapped[str | None] = mapped_column(String(64), default=None, index=True)

    status: Mapped[str] = mapped_column(String(16), default="scheduled", nullable=False)
    attendance_opened_at: Mapped[datetime | None] = mapped_column(default=None)
    attendance_closed_at: Mapped[datetime | None] = mapped_column(default=None)
    #: One-time code / QR payload for self check-in.
    checkin_code: Mapped[str | None] = mapped_column(String(32), default=None, index=True)

    material_keys: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    tags: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    participants: Mapped[list[ActivityParticipant]] = relationship(
        back_populates="activity", cascade="all, delete-orphan", passive_deletes=True
    )
    attendance_records: Mapped[list[AttendanceRecord]] = relationship(
        primaryjoin="AcademicActivity.id == foreign(AttendanceRecord.activity_id)",
        viewonly=True,
    )


class ActivityParticipant(Base, IdMixin, TimestampMixin, SyncMixin):
    """A person's involvement in an academic activity."""

    __tablename__ = "activity_participants"
    __table_args__ = (
        UniqueConstraint("activity_id", "user_id", "role", name="uq_activity_participant"),
        Index("ix_activity_participants_user", "user_id", "role"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    activity_id: Mapped[str] = mapped_column(
        ForeignKey("academic_activities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(24), default=ParticipantRole.ATTENDEE, nullable=False)
    attended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    checked_in_at: Mapped[datetime | None] = mapped_column(default=None)
    minutes_present: Mapped[int | None] = mapped_column(default=None)
    contribution_note: Mapped[str | None] = mapped_column(Text, default=None)
    #: Credit awarded for this participation; presenters typically earn more.
    credits_awarded: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    verified_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    activity: Mapped[AcademicActivity] = relationship(back_populates="participants")
    user: Mapped[User] = relationship(foreign_keys=[user_id])


class ConferenceRecord(Base, IdMixin, TimestampMixin, SyncMixin):
    """External conference or course attendance, evidenced by certificate upload."""

    __tablename__ = "conference_records"
    __table_args__ = (Index("ix_conference_records_user", "user_id", "start_date"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str | None] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), default=None, index=True
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    organiser: Mapped[str | None] = mapped_column(String(200), default=None)
    scope: Mapped[str] = mapped_column(String(24), default="national", nullable=False)  # local|national|international
    city: Mapped[str | None] = mapped_column(String(120), default=None)
    country: Mapped[str | None] = mapped_column(String(80), default=None)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    participation: Mapped[str] = mapped_column(String(32), default="attendee", nullable=False)
    presentation_title: Mapped[str | None] = mapped_column(String(300), default=None)
    cme_credits: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    certificate_key: Mapped[str | None] = mapped_column(String(512), default=None)
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.SUBMITTED, nullable=False)
    verified_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
