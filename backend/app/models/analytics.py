"""Score snapshots, promotion decisions and accreditation profiles."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Date, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SyncMixin, TimestampMixin
from app.models.enums import PromotionOutcome, RagStatus

if TYPE_CHECKING:
    from app.models.training import Enrolment


class ScoreSnapshot(Base, IdMixin, TimestampMixin):
    """A computed performance picture for one trainee at a point in time.

    Snapshots are immutable and append-only, so trend lines and audit are both possible
    and no dashboard ever recomputes history.
    """

    __tablename__ = "score_snapshots"
    __table_args__ = (
        Index("ix_score_snapshots_enrolment_time", "enrolment_id", "computed_at"),
        Index("ix_score_snapshots_tenant_rag", "tenant_id", "overall_rag"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    computed_at: Mapped[datetime] = mapped_column(nullable=False)
    training_year: Mapped[int] = mapped_column(default=1, nullable=False)
    #: scheduled | on_demand | promotion_review | rotation_close
    trigger: Mapped[str] = mapped_column(String(32), default="scheduled", nullable=False)

    clinical_competency_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    research_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    academic_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    professionalism_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    leadership_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    attendance_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    teaching_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    exam_readiness_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False, index=True)
    promotion_readiness_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    overall_rag: Mapped[str] = mapped_column(String(8), default=RagStatus.UNKNOWN, nullable=False)
    #: {"clinical_competency": "green", "research": "amber", ...}
    domain_rag: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: Full requirement evaluation: every rule with measured vs. target and pass/fail.
    requirement_results: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    #: Human-readable gaps, ordered by severity, for the dashboard and remediation plan.
    gaps: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    #: Raw measured counts used by the calculation, kept for explainability.
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: The weights actually applied, copied from the curriculum version.
    weights_used: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    enrolment: Mapped[Enrolment] = relationship()


class PromotionReview(Base, IdMixin, TimestampMixin, SyncMixin):
    """A promotion or completion decision, recommended by the engine and ratified by people."""

    __tablename__ = "promotion_reviews"
    __table_args__ = (
        Index("ix_promotion_reviews_enrolment", "enrolment_id", "review_date"),
        Index("ix_promotion_reviews_outcome", "tenant_id", "outcome"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("score_snapshots.id", ondelete="SET NULL"), default=None
    )

    review_date: Mapped[date] = mapped_column(Date, nullable=False)
    from_level: Mapped[str] = mapped_column(String(32), nullable=False)
    to_level: Mapped[str] = mapped_column(String(32), nullable=False)
    from_year: Mapped[int] = mapped_column(default=1, nullable=False)
    to_year: Mapped[int] = mapped_column(default=2, nullable=False)

    #: What the engine concluded, before any human input.
    engine_outcome: Mapped[str] = mapped_column(
        String(24), default=PromotionOutcome.NOT_RECOMMENDED, nullable=False
    )
    engine_readiness_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Unmet mandatory requirements blocking promotion.
    blocking_requirements: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    engine_rationale: Mapped[str | None] = mapped_column(Text, default=None)

    outcome: Mapped[str | None] = mapped_column(String(24), default=None)
    decided_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    committee: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    conditions: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    effective_date: Mapped[date | None] = mapped_column(Date, default=None)
    decision_note: Mapped[str | None] = mapped_column(Text, default=None)
    #: Set when a human decision differs from the engine recommendation; requires a reason.
    override_reason: Mapped[str | None] = mapped_column(Text, default=None)

    enrolment: Mapped[Enrolment] = relationship()


class AccreditationProfile(Base, IdMixin, TimestampMixin, SyncMixin):
    """A body's accreditation standard, expressed as evaluable requirements.

    Adding a new accrediting body — or absorbing a mid-cycle standard revision — is a
    data exercise: create a profile, attach ``AccreditationCriterion`` rows, generate.
    """

    __tablename__ = "accreditation_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_accreditation_profile_code"),
    )

    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), default=None, index=True
    )
    body: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    body_name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(32), default="1.0", nullable=False)
    applies_to_programme_types: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date, default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    #: Report layout definition — sections, ordering, narrative prompts.
    report_template: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    criteria: Mapped[list[AccreditationCriterion]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AccreditationCriterion.sort_order",
    )


class AccreditationCriterion(Base, IdMixin, TimestampMixin, SyncMixin):
    """One measurable accreditation standard.

    ``metric`` names a measurement the accreditation service knows how to compute over
    a department (e.g. ``consultant_count``, ``annual_major_operations``,
    ``icu_beds``, ``trainee_publication_rate``, ``academic_activity_frequency``).
    """

    __tablename__ = "accreditation_criteria"
    __table_args__ = (Index("ix_accreditation_criteria_profile", "profile_id", "sort_order"),)

    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), default=None, index=True
    )
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("accreditation_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    section: Mapped[str] = mapped_column(String(120), default="general", nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str] = mapped_column(String(8), default="gte", nullable=False)
    target_value: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(48), default=None)
    parameters: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: essential | desirable | informational
    weighting: Mapped[str] = mapped_column(String(24), default="essential", nullable=False)
    evidence_guidance: Mapped[str | None] = mapped_column(Text, default=None)
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)

    profile: Mapped[AccreditationProfile] = relationship(back_populates="criteria")


class AccreditationReview(Base, IdMixin, TimestampMixin, SyncMixin):
    """A generated accreditation return for one department against one profile."""

    __tablename__ = "accreditation_reviews"
    __table_args__ = (Index("ix_accreditation_reviews_org", "org_unit_id", "generated_at"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_unit_id: Mapped[str] = mapped_column(
        ForeignKey("org_units.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("accreditation_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(nullable=False)
    generated_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    #: Per-criterion evaluation with measured value, target and verdict.
    criterion_results: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    essential_met: Mapped[int] = mapped_column(default=0, nullable=False)
    essential_total: Mapped[int] = mapped_column(default=0, nullable=False)
    compliance_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    readiness_rag: Mapped[str] = mapped_column(String(8), default=RagStatus.UNKNOWN, nullable=False)
    gaps: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    narrative: Mapped[str | None] = mapped_column(Text, default=None)
    export_key: Mapped[str | None] = mapped_column(String(512), default=None)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)


class AccreditationEvidence(Base, IdMixin, TimestampMixin, SyncMixin):
    """Supporting evidence attached against a criterion."""

    __tablename__ = "accreditation_evidence"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_unit_id: Mapped[str] = mapped_column(
        ForeignKey("org_units.id", ondelete="CASCADE"), nullable=False, index=True
    )
    criterion_id: Mapped[str | None] = mapped_column(
        ForeignKey("accreditation_criteria.id", ondelete="SET NULL"), default=None, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    valid_from: Mapped[date | None] = mapped_column(Date, default=None)
    valid_to: Mapped[date | None] = mapped_column(Date, default=None)
    uploaded_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
