"""The digital logbook: clinical activity records requiring consultant validation."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Column, Date, Float, ForeignKey, Index, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SoftDeleteMixin, SyncMixin, TimestampMixin
from app.models.enums import (
    CaseComplexity,
    CaseOutcome,
    LogEntryType,
    ParticipationRole,
    ValidationStatus,
)

if TYPE_CHECKING:
    from app.models.curriculum import Competency, ProcedureCatalogueItem
    from app.models.identity import User
    from app.models.training import Enrolment, RotationAssignment


#: Many-to-many between a logbook entry and the competencies it evidences.
log_entry_competencies = Table(
    "log_entry_competencies",
    Base.metadata,
    Column("log_entry_id", ForeignKey("log_entries.id", ondelete="CASCADE"), primary_key=True),
    Column("competency_id", ForeignKey("competencies.id", ondelete="CASCADE"), primary_key=True),
)


class LogEntry(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    """A single clinical, academic or procedural activity performed by a trainee.

    Patient identity is never stored. ``patient_reference`` holds a pseudonymised,
    institution-local token (hospital number hashed with a tenant salt) so that entries
    can be de-duplicated and audited without holding identifiable patient data.
    """

    __tablename__ = "log_entries"
    __table_args__ = (
        Index("ix_log_entries_enrolment_date", "enrolment_id", "occurred_at"),
        Index("ix_log_entries_validation", "tenant_id", "validation_status"),
        Index("ix_log_entries_supervisor_pending", "supervisor_id", "validation_status"),
        Index("ix_log_entries_type", "enrolment_id", "entry_type"),
        Index("ix_log_entries_procedure", "procedure_id"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rotation_assignment_id: Mapped[str | None] = mapped_column(
        ForeignKey("rotation_assignments.id", ondelete="SET NULL"), default=None, index=True
    )
    org_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="SET NULL"), default=None, index=True
    )

    entry_type: Mapped[str] = mapped_column(String(32), default=LogEntryType.ADMISSION, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, default=None)

    # -- de-identified patient context ------------------------------------
    patient_reference: Mapped[str | None] = mapped_column(String(128), default=None, index=True)
    patient_age_years: Mapped[int | None] = mapped_column(default=None)
    patient_age_months: Mapped[int | None] = mapped_column(default=None)
    patient_sex: Mapped[str | None] = mapped_column(String(16), default=None)
    setting: Mapped[str | None] = mapped_column(String(64), default=None)  # ward | theatre | clinic | A&E

    # -- clinical content --------------------------------------------------
    diagnosis: Mapped[str | None] = mapped_column(String(300), default=None)
    #: Structured coding: {"icd10": ["K35.8"], "snomed": [...]}
    diagnosis_codes: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    procedure_id: Mapped[str | None] = mapped_column(
        ForeignKey("procedure_catalogue.id", ondelete="SET NULL"), default=None
    )
    procedure_name: Mapped[str | None] = mapped_column(String(255), default=None)
    procedure_grade: Mapped[str | None] = mapped_column(String(24), default=None, index=True)
    participation_role: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    complexity: Mapped[str] = mapped_column(String(24), default=CaseComplexity.ROUTINE, nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), default=CaseOutcome.UNKNOWN, nullable=False)
    complication_detail: Mapped[str | None] = mapped_column(Text, default=None)
    anaesthesia_type: Mapped[str | None] = mapped_column(String(64), default=None)
    duration_minutes: Mapped[int | None] = mapped_column(default=None)
    quantity: Mapped[int] = mapped_column(default=1, nullable=False)

    # -- learning ----------------------------------------------------------
    reflection: Mapped[str | None] = mapped_column(Text, default=None)
    learning_points: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    attachment_keys: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    # -- validation --------------------------------------------------------
    supervisor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )
    validation_status: Mapped[str] = mapped_column(
        String(16), default=ValidationStatus.PENDING, nullable=False
    )
    validated_at: Mapped[datetime | None] = mapped_column(default=None)
    validated_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    validator_comment: Mapped[str | None] = mapped_column(Text, default=None)
    #: Number of times the entry was returned for correction — a professionalism signal.
    query_count: Mapped[int] = mapped_column(default=0, nullable=False)

    #: Set when the entry was created offline; retained for the sync audit trail.
    captured_offline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    enrolment: Mapped[Enrolment] = relationship()
    rotation_assignment: Mapped[RotationAssignment | None] = relationship()
    procedure: Mapped[ProcedureCatalogueItem | None] = relationship()
    supervisor: Mapped[User | None] = relationship(foreign_keys=[supervisor_id])
    competencies: Mapped[list[Competency]] = relationship(secondary=log_entry_competencies)

    @property
    def is_countable(self) -> bool:
        """Only validated entries count toward requirements and scores."""
        return self.validation_status == ValidationStatus.VALIDATED


class LogEntryAudit(Base, IdMixin, TimestampMixin):
    """Immutable trail of every state change on a logbook entry.

    Logbooks are evidentiary documents for college examinations; the platform must be
    able to prove who changed what and when.
    """

    __tablename__ = "log_entry_audits"
    __table_args__ = (Index("ix_log_entry_audits_entry", "log_entry_id", "created_at"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    log_entry_id: Mapped[str] = mapped_column(
        ForeignKey("log_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16), default=None)
    to_status: Mapped[str | None] = mapped_column(String(16), default=None)
    changes: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, default=None)


class TeachingRecord(Base, IdMixin, TimestampMixin, SyncMixin):
    """Teaching delivered by a trainee — feeds the Teaching Score."""

    __tablename__ = "teaching_records"
    __table_args__ = (Index("ix_teaching_records_enrolment", "enrolment_id", "occurred_on"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    audience: Mapped[str] = mapped_column(String(120), default="medical students", nullable=False)
    audience_size: Mapped[int] = mapped_column(default=0, nullable=False)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(default=60, nullable=False)
    format: Mapped[str] = mapped_column(String(64), default="tutorial", nullable=False)
    #: Mean learner feedback score out of 5, when collected.
    feedback_score: Mapped[float | None] = mapped_column(Float, default=None)
    verified_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    validation_status: Mapped[str] = mapped_column(
        String(16), default=ValidationStatus.PENDING, nullable=False
    )
    attachment_keys: Mapped[list[str]] = mapped_column(default=list, nullable=False)


#: Re-exported for convenience in requirement evaluation.
PARTICIPATION_ROLES = tuple(r.value for r in ParticipationRole)
LOG_ENTRY_TYPES = tuple(t.value for t in LogEntryType)
