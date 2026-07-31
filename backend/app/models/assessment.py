"""Workplace-based assessment, competency ratings and multi-source feedback."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Date, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SoftDeleteMixin, SyncMixin, TimestampMixin
from app.models.enums import (
    ApprovalStatus,
    AssessmentKind,
    AssessmentVerdict,
    EntrustmentLevel,
)

if TYPE_CHECKING:
    from app.models.curriculum import Competency
    from app.models.identity import User
    from app.models.training import Enrolment, RotationAssignment


class AssessmentTemplate(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    """A user-designed assessment instrument.

    ``form_schema`` is a declarative field list rendered dynamically by the client, so a
    department can invent a new assessment form without any frontend change::

        [{"key": "history", "label": "History taking", "type": "scale",
          "min": 1, "max": 9, "weight": 1.0, "required": true,
          "anchors": {"1": "Well below expectation", "9": "Outstanding"}},
         {"key": "comment", "label": "Overall comment", "type": "textarea"}]
    """

    __tablename__ = "assessment_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_assessment_template_code"),
        Index("ix_assessment_templates_kind", "tenant_id", "kind"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Optional restriction to a department/programme; NULL means institution-wide.
    org_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="CASCADE"), default=None, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default=AssessmentKind.MINI_CEX, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    instructions: Mapped[str | None] = mapped_column(Text, default=None)

    form_schema: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    #: {"method": "weighted_mean", "pass_mark": 60, "scale_max": 9,
    #:  "verdict_bands": {"below_expectation": 40, "borderline": 55, ...}}
    scoring_config: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: Competency codes this instrument is designed to evidence.
    competency_codes: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    requires_trainee_reflection: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    requires_countersign: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    min_assessors: Mapped[int] = mapped_column(default=1, nullable=False)
    applies_to_levels: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)

    assessments: Mapped[list[Assessment]] = relationship(
        back_populates="template", passive_deletes=True
    )


class Assessment(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    """A completed (or in-flight) assessment of a trainee."""

    __tablename__ = "assessments"
    __table_args__ = (
        Index("ix_assessments_enrolment_date", "enrolment_id", "occurred_on"),
        Index("ix_assessments_assessor_status", "assessor_id", "status"),
        Index("ix_assessments_rotation", "rotation_assignment_id"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_templates.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    enrolment_id: Mapped[str] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rotation_assignment_id: Mapped[str | None] = mapped_column(
        ForeignKey("rotation_assignments.id", ondelete="SET NULL"), default=None
    )
    assessor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )
    #: For multi-source feedback the assessor may be a nurse, peer or patient advocate.
    assessor_relationship: Mapped[str | None] = mapped_column(String(64), default=None)

    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    setting: Mapped[str | None] = mapped_column(String(120), default=None)
    case_summary: Mapped[str | None] = mapped_column(Text, default=None)
    case_complexity: Mapped[str | None] = mapped_column(String(24), default=None)

    #: {field_key: value} matching ``AssessmentTemplate.form_schema``.
    responses: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    raw_score: Mapped[float | None] = mapped_column(Float, default=None)
    max_score: Mapped[float | None] = mapped_column(Float, default=None)
    percent_score: Mapped[float | None] = mapped_column(Float, default=None, index=True)
    verdict: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    is_pass: Mapped[bool | None] = mapped_column(Boolean, default=None)

    strengths: Mapped[str | None] = mapped_column(Text, default=None)
    development_needs: Mapped[str | None] = mapped_column(Text, default=None)
    agreed_actions: Mapped[str | None] = mapped_column(Text, default=None)
    trainee_reflection: Mapped[str | None] = mapped_column(Text, default=None)

    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.DRAFT, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(default=None)
    trainee_acknowledged_at: Mapped[datetime | None] = mapped_column(default=None)
    countersigned_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    countersigned_at: Mapped[datetime | None] = mapped_column(default=None)
    attachment_keys: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    template: Mapped[AssessmentTemplate] = relationship(back_populates="assessments")
    enrolment: Mapped[Enrolment] = relationship()
    rotation_assignment: Mapped[RotationAssignment | None] = relationship()
    assessor: Mapped[User | None] = relationship(foreign_keys=[assessor_id])
    competency_ratings: Mapped[list[CompetencyRating]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_final(self) -> bool:
        return self.status == ApprovalStatus.APPROVED


class CompetencyRating(Base, IdMixin, TimestampMixin, SyncMixin):
    """An entrustment decision against one competency/EPA at a point in time.

    Ratings are append-only: progression is measured by comparing the latest rating for
    each competency against the curriculum's target for the trainee's year.
    """

    __tablename__ = "competency_ratings"
    __table_args__ = (
        Index("ix_competency_ratings_enrolment", "enrolment_id", "competency_id", "rated_on"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    competency_id: Mapped[str] = mapped_column(
        ForeignKey("competencies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assessment_id: Mapped[str | None] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), default=None, index=True
    )
    rotation_assignment_id: Mapped[str | None] = mapped_column(
        ForeignKey("rotation_assignments.id", ondelete="SET NULL"), default=None
    )
    assessor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    level: Mapped[str] = mapped_column(
        String(32), default=EntrustmentLevel.DIRECT_SUPERVISION, nullable=False
    )
    #: Numeric mirror of ``level`` (1-5) so aggregates avoid string ordering.
    level_value: Mapped[int] = mapped_column(default=2, nullable=False, index=True)
    rated_on: Mapped[date] = mapped_column(Date, nullable=False)
    confidence: Mapped[str | None] = mapped_column(String(24), default=None)
    evidence: Mapped[str | None] = mapped_column(Text, default=None)
    is_self_rating: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    assessment: Mapped[Assessment | None] = relationship(back_populates="competency_ratings")
    competency: Mapped[Competency] = relationship()


class MultiSourceFeedbackRound(Base, IdMixin, TimestampMixin, SyncMixin):
    """A 360° feedback exercise: one round, many anonymous respondents."""

    __tablename__ = "msf_rounds"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_templates.id", ondelete="RESTRICT"), nullable=False
    )
    opened_on: Mapped[date] = mapped_column(Date, nullable=False)
    closes_on: Mapped[date] = mapped_column(Date, nullable=False)
    invited_count: Mapped[int] = mapped_column(default=0, nullable=False)
    responded_count: Mapped[int] = mapped_column(default=0, nullable=False)
    min_responses_required: Mapped[int] = mapped_column(default=8, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.DRAFT, nullable=False)
    #: Aggregate only — individual responses are never exposed to the trainee.
    aggregate: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(default=None)


VERDICT_ORDER: tuple[str, ...] = (
    AssessmentVerdict.BELOW_EXPECTATION,
    AssessmentVerdict.BORDERLINE,
    AssessmentVerdict.MEETS_EXPECTATION,
    AssessmentVerdict.ABOVE_EXPECTATION,
    AssessmentVerdict.OUTSTANDING,
)
