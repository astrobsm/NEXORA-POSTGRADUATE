"""Duty rosters, shifts, swaps and attendance."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Date, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SyncMixin, TimestampMixin
from app.models.enums import (
    ApprovalStatus,
    AttendanceStatus,
    DutyKind,
    ShiftStatus,
)

if TYPE_CHECKING:
    from app.models.identity import User


class DutyRoster(Base, IdMixin, TimestampMixin, SyncMixin):
    """A published roster covering a period for one organisational unit."""

    __tablename__ = "duty_rosters"
    __table_args__ = (Index("ix_duty_rosters_org_period", "org_unit_id", "period_start"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_unit_id: Mapped[str] = mapped_column(
        ForeignKey("org_units.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.DRAFT, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(default=None)
    published_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    #: Generation constraints: min rest hours, max consecutive nights, calls per person,
    #: exclusion dates, fairness weighting. Consumed by the roster generator.
    generation_config: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    shifts: Mapped[list[DutyShift]] = relationship(
        back_populates="roster",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DutyShift.starts_at",
    )


class DutyShift(Base, IdMixin, TimestampMixin, SyncMixin):
    __tablename__ = "duty_shifts"
    __table_args__ = (
        Index("ix_duty_shifts_user_time", "user_id", "starts_at"),
        Index("ix_duty_shifts_org_time", "org_unit_id", "starts_at"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    roster_id: Mapped[str | None] = mapped_column(
        ForeignKey("duty_rosters.id", ondelete="CASCADE"), default=None, index=True
    )
    org_unit_id: Mapped[str] = mapped_column(
        ForeignKey("org_units.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    duty_kind: Mapped[str] = mapped_column(String(24), default=DutyKind.WARD, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(nullable=False)
    ends_at: Mapped[datetime] = mapped_column(nullable=False)
    location: Mapped[str | None] = mapped_column(String(160), default=None)
    #: The senior on call with this trainee.
    supervising_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[str] = mapped_column(String(16), default=ShiftStatus.SCHEDULED, nullable=False)
    is_public_holiday: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Difficulty/intensity multiplier feeding the duty performance score.
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    roster: Mapped[DutyRoster | None] = relationship(back_populates="shifts")
    user: Mapped[User] = relationship(foreign_keys=[user_id])
    attendance: Mapped[list[AttendanceRecord]] = relationship(
        back_populates="shift", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def hours(self) -> float:
        return (self.ends_at - self.starts_at).total_seconds() / 3600.0


class DutySwapRequest(Base, IdMixin, TimestampMixin, SyncMixin):
    __tablename__ = "duty_swap_requests"
    __table_args__ = (Index("ix_duty_swaps_status", "tenant_id", "status"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requester_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    counterparty_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )
    offered_shift_id: Mapped[str] = mapped_column(
        ForeignKey("duty_shifts.id", ondelete="CASCADE"), nullable=False
    )
    requested_shift_id: Mapped[str | None] = mapped_column(
        ForeignKey("duty_shifts.id", ondelete="CASCADE"), default=None
    )
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.SUBMITTED, nullable=False)
    counterparty_accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approver_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    decision_note: Mapped[str | None] = mapped_column(Text, default=None)


class AttendanceRecord(Base, IdMixin, TimestampMixin, SyncMixin):
    """Attendance against either a duty shift or an academic activity.

    A single table keeps the attendance percentage calculations uniform across duty and
    academic requirements.
    """

    __tablename__ = "attendance_records"
    __table_args__ = (
        Index("ix_attendance_user_time", "user_id", "recorded_for"),
        Index("ix_attendance_shift", "shift_id"),
        Index("ix_attendance_activity", "activity_id"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shift_id: Mapped[str | None] = mapped_column(
        ForeignKey("duty_shifts.id", ondelete="CASCADE"), default=None
    )
    activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("academic_activities.id", ondelete="CASCADE"), default=None
    )

    recorded_for: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=AttendanceStatus.PRESENT, nullable=False)
    check_in_at: Mapped[datetime | None] = mapped_column(default=None)
    check_out_at: Mapped[datetime | None] = mapped_column(default=None)
    minutes_late: Mapped[int] = mapped_column(default=0, nullable=False)
    #: qr | geo | manual | biometric | self_declared
    capture_method: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    geo: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    recorded_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    excuse_reason: Mapped[str | None] = mapped_column(Text, default=None)
    excuse_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    shift: Mapped[DutyShift | None] = relationship(back_populates="attendance")

    @property
    def counts_as_present(self) -> bool:
        return self.status in {
            AttendanceStatus.PRESENT,
            AttendanceStatus.LATE,
            AttendanceStatus.PARTIAL,
            AttendanceStatus.EXCUSED,
        }
