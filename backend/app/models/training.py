"""Enrolment, rotation assignment and leave — the trainee lifecycle."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Date, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SoftDeleteMixin, SyncMixin, TimestampMixin
from app.models.enums import (
    ApprovalStatus,
    EnrolmentStatus,
    LeaveType,
    RotationStatus,
    TrainingLevel,
)

if TYPE_CHECKING:
    from app.models.curriculum import Programme
    from app.models.identity import User


class Enrolment(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    """A trainee's registration on a programme, pinned to a curriculum version."""

    __tablename__ = "enrolments"
    __table_args__ = (
        UniqueConstraint("trainee_id", "programme_id", "cohort_year", name="uq_enrolment_unique"),
        Index("ix_enrolments_tenant_status", "tenant_id", "status"),
        Index("ix_enrolments_programme_year", "programme_id", "current_year"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trainee_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    programme_id: Mapped[str] = mapped_column(
        ForeignKey("programmes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    curriculum_version_id: Mapped[str] = mapped_column(
        ForeignKey("curriculum_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    org_unit_id: Mapped[str] = mapped_column(
        ForeignKey("org_units.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Primary clinical/academic supervisor for the whole enrolment.
    primary_supervisor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )

    registration_number: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    #: College-side candidate number (NPMCN/WACS/WACP).
    college_number: Mapped[str | None] = mapped_column(String(64), default=None)
    cohort_year: Mapped[int] = mapped_column(nullable=False)
    current_level: Mapped[str] = mapped_column(
        String(32), default=TrainingLevel.REGISTRAR, nullable=False
    )
    current_year: Mapped[int] = mapped_column(default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=EnrolmentStatus.ACTIVE, nullable=False)

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    expected_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    actual_end_date: Mapped[date | None] = mapped_column(Date, default=None)
    #: Cumulative days of approved interruption, used to extend the expected end date.
    interruption_days: Mapped[int] = mapped_column(default=0, nullable=False)
    extension_months: Mapped[int] = mapped_column(default=0, nullable=False)

    #: Denormalised latest overall score & RAG for fast dashboard lists. Recomputed by
    #: ``app.services.scoring``; never a source of truth.
    latest_overall_score: Mapped[float | None] = mapped_column(default=None)
    latest_rag: Mapped[str | None] = mapped_column(String(8), default=None, index=True)
    promotion_ready: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_scored_at: Mapped[datetime | None] = mapped_column(default=None)

    notes: Mapped[str | None] = mapped_column(Text, default=None)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    trainee: Mapped[User] = relationship(back_populates="enrolments", foreign_keys=[trainee_id])
    programme: Mapped[Programme] = relationship(back_populates="enrolments")
    primary_supervisor: Mapped[User | None] = relationship(foreign_keys=[primary_supervisor_id])
    rotations: Mapped[list[RotationAssignment]] = relationship(
        back_populates="enrolment",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RotationAssignment.start_date",
    )
    leave_records: Mapped[list[LeaveRecord]] = relationship(
        back_populates="enrolment", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_active(self) -> bool:
        return self.status in {EnrolmentStatus.ACTIVE, EnrolmentStatus.ON_LEAVE}

    def current_rotation(self, on: date) -> RotationAssignment | None:
        for rotation in self.rotations:
            if rotation.start_date <= on <= rotation.end_date and rotation.status in {
                RotationStatus.ACTIVE,
                RotationStatus.EXTENDED,
                RotationStatus.REMEDIAL,
            }:
                return rotation
        return None


class RotationAssignment(Base, IdMixin, TimestampMixin, SyncMixin):
    """A trainee actually posted to a unit for a period."""

    __tablename__ = "rotation_assignments"
    __table_args__ = (
        Index("ix_rotation_assignments_enrolment_dates", "enrolment_id", "start_date", "end_date"),
        Index("ix_rotation_assignments_supervisor", "supervisor_id", "status"),
        Index("ix_rotation_assignments_org_dates", "org_unit_id", "start_date"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rotation_template_id: Mapped[str | None] = mapped_column(
        ForeignKey("rotation_templates.id", ondelete="SET NULL"), default=None, index=True
    )
    org_unit_id: Mapped[str] = mapped_column(
        ForeignKey("org_units.id", ondelete="CASCADE"), nullable=False
    )
    supervisor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    training_year: Mapped[int] = mapped_column(default=1, nullable=False)
    sequence: Mapped[int] = mapped_column(default=1, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    original_end_date: Mapped[date | None] = mapped_column(Date, default=None)
    status: Mapped[str] = mapped_column(String(16), default=RotationStatus.PLANNED, nullable=False)

    is_elective: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_remedial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: When a rotation is repeated, points at the assignment being remediated.
    remediates_id: Mapped[str | None] = mapped_column(
        ForeignKey("rotation_assignments.id", ondelete="SET NULL"), default=None
    )
    extension_reason: Mapped[str | None] = mapped_column(Text, default=None)

    objectives: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    #: Snapshot of requirement evaluation at closure — {"met": [...], "unmet": [...]}
    completion_summary: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    completion_percent: Mapped[float] = mapped_column(default=0.0, nullable=False)
    supervisor_comment: Mapped[str | None] = mapped_column(Text, default=None)
    closed_at: Mapped[datetime | None] = mapped_column(default=None)
    closed_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    enrolment: Mapped[Enrolment] = relationship(back_populates="rotations")
    supervisor: Mapped[User | None] = relationship(foreign_keys=[supervisor_id])

    @property
    def duration_days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    def overlaps(self, other: RotationAssignment) -> bool:
        return self.start_date <= other.end_date and other.start_date <= self.end_date


class LeaveRecord(Base, IdMixin, TimestampMixin, SyncMixin):
    """Leave, interruption or absence that may extend the training end date."""

    __tablename__ = "leave_records"
    __table_args__ = (Index("ix_leave_records_enrolment_dates", "enrolment_id", "start_date"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    leave_type: Mapped[str] = mapped_column(String(24), default=LeaveType.ANNUAL, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: Whether this leave counts against the training clock.
    extends_training: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.SUBMITTED, nullable=False)
    approver_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    decision_note: Mapped[str | None] = mapped_column(Text, default=None)
    attachment_keys: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    enrolment: Mapped[Enrolment] = relationship(back_populates="leave_records")

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1


class TransferRecord(Base, IdMixin, TimestampMixin):
    """Movement of a trainee between institutions or departments, preserving history."""

    __tablename__ = "transfer_records"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_org_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="SET NULL"), default=None
    )
    to_org_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="SET NULL"), default=None
    )
    to_tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), default=None
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.SUBMITTED, nullable=False)
    #: Credit carried across: months, procedures, competencies recognised.
    credit_transferred: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    approved_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
