"""Continuing medical education: curated resources, assignments and credits."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Date, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SoftDeleteMixin, SyncMixin, TimestampMixin
from app.models.enums import (
    AuthoringSource,
    CmeResourceKind,
    CmeStatus,
    EditorialStatus,
)

if TYPE_CHECKING:
    from app.models.identity import User


class CmeResource(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    """A guideline, article, video, book, podcast or evidence summary."""

    __tablename__ = "cme_resources"
    __table_args__ = (
        Index("ix_cme_resources_specialty", "tenant_id", "specialty_id"),
        Index("ix_cme_resources_kind", "tenant_id", "kind"),
    )

    #: NULL tenant == platform-curated resource available to every institution.
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), default=None, index=True
    )
    specialty_id: Mapped[str | None] = mapped_column(
        ForeignKey("specialties.id", ondelete="SET NULL"), default=None
    )
    kind: Mapped[str] = mapped_column(
        String(32), default=CmeResourceKind.JOURNAL_ARTICLE, nullable=False
    )
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    authors: Mapped[str | None] = mapped_column(String(500), default=None)
    source: Mapped[str | None] = mapped_column(String(300), default=None)  # journal / publisher
    year: Mapped[int | None] = mapped_column(default=None)
    doi: Mapped[str | None] = mapped_column(String(160), default=None, index=True)
    url: Mapped[str | None] = mapped_column(String(1024), default=None)
    #: Object-storage key when the file is hosted by the institution.
    object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    abstract: Mapped[str | None] = mapped_column(Text, default=None)
    #: AI- or editor-produced key points for rapid review.
    key_points: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    estimated_minutes: Mapped[int] = mapped_column(default=20, nullable=False)
    credits: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    level: Mapped[str] = mapped_column(String(24), default="all", nullable=False)
    applies_to_levels: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    topics: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    tags: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    evidence_level: Mapped[str | None] = mapped_column(String(24), default=None)

    #: Optional short quiz proving engagement: [{"q": "...", "options": [...], "answer": 1}]
    assessment_items: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    pass_mark_percent: Mapped[float] = mapped_column(Float, default=70.0, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    added_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    # ---- Provenance and the publication gate -----------------------------
    authoring_source: Mapped[str] = mapped_column(
        String(24), default=AuthoringSource.HUMAN, nullable=False, index=True
    )
    #: Trainees are only ever assigned PUBLISHED resources. AI-written articles
    #: enter at AI_DRAFT and stay invisible until a consultant approves them.
    editorial_status: Mapped[str] = mapped_column(
        String(24), default=EditorialStatus.PUBLISHED, nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    reviewed_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(default=None)
    review_notes: Mapped[str | None] = mapped_column(Text, default=None)
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), default=None
    )
    ai_confidence: Mapped[float | None] = mapped_column(Float, default=None)

    # ---- Structured article body -----------------------------------------
    #: The full article, section by section, in the order the curriculum
    #: prescribes. Stored as an ordered list rather than 26 columns so an
    #: institution can drop sections that do not apply to its discipline —
    #: a radiology article has no operative technique — without a migration.
    #: [{"key": "anatomy", "title": "Anatomy", "body": "...", "order": 4}, ...]
    sections: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    #: Structured references, each carrying both citation renderings and a DOI.
    #: [{"n": 1, "vancouver": "...", "apa": "...", "doi": "...", "source": "..."}]
    reference_entries: Mapped[list[dict[str, Any]]] = mapped_column(
        default=list, nullable=False
    )
    learning_objectives: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    clinical_pearls: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    #: Areas examiners return to, used to weight generated items toward them.
    frequently_tested_areas: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    landmark_trials: Mapped[list[dict[str, Any]]] = mapped_column(
        default=list, nullable=False
    )
    #: Word count of the assembled body, used to sanity-check reading time.
    word_count: Mapped[int] = mapped_column(default=0, nullable=False)

    assignments: Mapped[list[CmeAssignment]] = relationship(
        back_populates="resource", passive_deletes=True
    )

    @property
    def is_servable(self) -> bool:
        """Whether this resource may be assigned or shown to a trainee."""
        return (
            self.is_active
            and self.deleted_at is None
            and self.editorial_status == EditorialStatus.PUBLISHED
        )

    @property
    def section_keys(self) -> list[str]:
        return [str(s.get("key")) for s in self.sections if s.get("key")]


class CmeAssignment(Base, IdMixin, TimestampMixin, SyncMixin):
    """A resource assigned to a trainee — manually, by rule, or by the AI recommender."""

    __tablename__ = "cme_assignments"
    __table_args__ = (
        UniqueConstraint("user_id", "resource_id", "assigned_on", name="uq_cme_assignment"),
        Index("ix_cme_assignments_user_status", "user_id", "status"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str | None] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), default=None, index=True
    )
    resource_id: Mapped[str] = mapped_column(
        ForeignKey("cme_resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assigned_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    #: manual | curriculum_rule | rotation | ai_recommendation | remediation
    assignment_source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, default=None)

    assigned_on: Mapped[date] = mapped_column(Date, nullable=False)
    due_on: Mapped[date | None] = mapped_column(Date, default=None, index=True)
    status: Mapped[str] = mapped_column(String(16), default=CmeStatus.ASSIGNED, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)
    seconds_spent: Mapped[int] = mapped_column(default=0, nullable=False)
    progress_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    assessment_score: Mapped[float | None] = mapped_column(Float, default=None)
    assessment_attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    credits_awarded: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reflection: Mapped[str | None] = mapped_column(Text, default=None)
    certificate_key: Mapped[str | None] = mapped_column(String(512), default=None)

    resource: Mapped[CmeResource] = relationship(back_populates="assignments")
    user: Mapped[User] = relationship(foreign_keys=[user_id])


class CmeCreditLedger(Base, IdMixin, TimestampMixin):
    """Append-only ledger of CME credits, the auditable basis for annual returns."""

    __tablename__ = "cme_credit_ledger"
    __table_args__ = (Index("ix_cme_ledger_user_period", "user_id", "period_year"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period_year: Mapped[int] = mapped_column(nullable=False)
    #: cme_assignment | academic_activity | conference | teaching | publication | manual
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(32), default=None)
    description: Mapped[str] = mapped_column(String(400), nullable=False)
    credits: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    awarded_on: Mapped[date] = mapped_column(Date, nullable=False)
    awarded_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    #: Which body recognises this credit: mdcn | npmcn | wacs | wacp | internal
    recognised_by: Mapped[str] = mapped_column(String(32), default="internal", nullable=False)
    is_reversed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reversal_reason: Mapped[str | None] = mapped_column(Text, default=None)
