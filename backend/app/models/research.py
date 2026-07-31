"""Research projects, the dissertation workflow, ethics and publications."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Date, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SoftDeleteMixin, SyncMixin, TimestampMixin
from app.models.enums import (
    ApprovalStatus,
    DissertationStage,
    PublicationType,
    ResearchType,
)

if TYPE_CHECKING:
    from app.models.identity import User
    from app.models.training import Enrolment


class ResearchProject(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    """A dissertation, thesis, audit or other scholarly project."""

    __tablename__ = "research_projects"
    __table_args__ = (
        Index("ix_research_projects_enrolment", "enrolment_id"),
        Index("ix_research_projects_stage", "tenant_id", "current_stage"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_unit_id: Mapped[str] = mapped_column(
        ForeignKey("org_units.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str | None] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), default=None, index=True
    )
    principal_investigator_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    research_type: Mapped[str] = mapped_column(
        String(32), default=ResearchType.DISSERTATION, nullable=False
    )
    #: The college the dissertation is submitted to (npmcn | wacs | wacp | ...).
    submitting_body: Mapped[str | None] = mapped_column(String(32), default=None)
    background: Mapped[str | None] = mapped_column(Text, default=None)
    aim: Mapped[str | None] = mapped_column(Text, default=None)
    objectives: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    methodology: Mapped[str | None] = mapped_column(Text, default=None)
    study_design: Mapped[str | None] = mapped_column(String(120), default=None)
    setting: Mapped[str | None] = mapped_column(String(200), default=None)
    sample_size: Mapped[int | None] = mapped_column(default=None)
    keywords: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    current_stage: Mapped[str] = mapped_column(
        String(32), default=DissertationStage.CONCEPT, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.DRAFT, nullable=False)
    started_on: Mapped[date | None] = mapped_column(Date, default=None)
    target_completion_on: Mapped[date | None] = mapped_column(Date, default=None)
    completed_on: Mapped[date | None] = mapped_column(Date, default=None)
    progress_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # -- ethics ------------------------------------------------------------
    ethics_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ethics_status: Mapped[str] = mapped_column(String(24), default="not_submitted", nullable=False)
    ethics_reference: Mapped[str | None] = mapped_column(String(120), default=None)
    ethics_committee: Mapped[str | None] = mapped_column(String(200), default=None)
    ethics_approved_on: Mapped[date | None] = mapped_column(Date, default=None)
    ethics_expires_on: Mapped[date | None] = mapped_column(Date, default=None)

    # -- funding -----------------------------------------------------------
    funding_source: Mapped[str | None] = mapped_column(String(200), default=None)
    funding_amount: Mapped[float | None] = mapped_column(Float, default=None)
    grant_reference: Mapped[str | None] = mapped_column(String(120), default=None)

    document_keys: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    enrolment: Mapped[Enrolment | None] = relationship()
    principal_investigator: Mapped[User] = relationship(foreign_keys=[principal_investigator_id])
    supervisions: Mapped[list[ProjectSupervision]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    milestones: Mapped[list[DissertationMilestone]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DissertationMilestone.sequence",
    )
    publications: Mapped[list[Publication]] = relationship(
        back_populates="project", passive_deletes=True
    )


class ProjectSupervision(Base, IdMixin, TimestampMixin, SyncMixin):
    """Supervisor attachment, including how the allocation was reached."""

    __tablename__ = "project_supervisions"
    __table_args__ = (
        UniqueConstraint("project_id", "supervisor_id", name="uq_project_supervisor"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("research_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supervisor_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    role: Mapped[str] = mapped_column(String(64), default="supervisor", nullable=False)
    assigned_on: Mapped[date] = mapped_column(Date, nullable=False)
    ended_on: Mapped[date | None] = mapped_column(Date, default=None)
    #: automatic | manual | trainee_request
    allocation_method: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    #: Explainability payload from the allocator: component scores and the final ranking.
    allocation_score: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(default=None)

    project: Mapped[ResearchProject] = relationship(back_populates="supervisions")
    supervisor: Mapped[User] = relationship(foreign_keys=[supervisor_id])


class DissertationMilestone(Base, IdMixin, TimestampMixin, SyncMixin):
    """One stage of the dissertation workflow, with its own approval gate."""

    __tablename__ = "dissertation_milestones"
    __table_args__ = (
        UniqueConstraint("project_id", "stage", name="uq_dissertation_milestone_stage"),
        Index("ix_dissertation_milestones_due", "tenant_id", "due_on"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("research_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    sequence: Mapped[int] = mapped_column(default=0, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    due_on: Mapped[date | None] = mapped_column(Date, default=None)
    submitted_on: Mapped[date | None] = mapped_column(Date, default=None)
    completed_on: Mapped[date | None] = mapped_column(Date, default=None)
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.DRAFT, nullable=False)
    approver_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    decision_note: Mapped[str | None] = mapped_column(Text, default=None)
    document_keys: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    #: Panel members for defence milestones.
    panel: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    outcome_score: Mapped[float | None] = mapped_column(Float, default=None)

    project: Mapped[ResearchProject] = relationship(back_populates="milestones")


class SupervisionMeeting(Base, IdMixin, TimestampMixin, SyncMixin):
    """Recorded supervisor–trainee progress meeting."""

    __tablename__ = "supervision_meetings"
    __table_args__ = (Index("ix_supervision_meetings_project", "project_id", "held_on"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("research_projects.id", ondelete="CASCADE"), default=None, index=True
    )
    enrolment_id: Mapped[str | None] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), default=None, index=True
    )
    supervisor_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    held_on: Mapped[date] = mapped_column(Date, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(default=30, nullable=False)
    agenda: Mapped[str | None] = mapped_column(Text, default=None)
    discussion: Mapped[str | None] = mapped_column(Text, default=None)
    agreed_actions: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    next_meeting_on: Mapped[date | None] = mapped_column(Date, default=None)
    trainee_confirmed_at: Mapped[datetime | None] = mapped_column(default=None)
    concerns_raised: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Publication(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    """A publication or presentation arising from research — feeds the Research Score."""

    __tablename__ = "publications"
    __table_args__ = (Index("ix_publications_user_year", "user_id", "year"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str | None] = mapped_column(
        ForeignKey("enrolments.id", ondelete="CASCADE"), default=None, index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("research_projects.id", ondelete="SET NULL"), default=None, index=True
    )

    publication_type: Mapped[str] = mapped_column(
        String(32), default=PublicationType.JOURNAL_ARTICLE, nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    authors: Mapped[str] = mapped_column(String(1000), nullable=False)
    #: 1 == first author. Weighted in the Research Score.
    author_position: Mapped[int] = mapped_column(default=1, nullable=False)
    is_corresponding: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    venue: Mapped[str | None] = mapped_column(String(300), default=None)  # journal / conference
    year: Mapped[int] = mapped_column(nullable=False)
    volume: Mapped[str | None] = mapped_column(String(32), default=None)
    pages: Mapped[str | None] = mapped_column(String(64), default=None)
    doi: Mapped[str | None] = mapped_column(String(160), default=None, index=True)
    url: Mapped[str | None] = mapped_column(String(1024), default=None)
    #: pubmed | scopus | web_of_science | ajol | google_scholar | none
    indexed_in: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    impact_factor: Mapped[float | None] = mapped_column(Float, default=None)
    is_peer_reviewed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    status: Mapped[str] = mapped_column(String(24), default="published", nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(16), default=ApprovalStatus.SUBMITTED, nullable=False
    )
    verified_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    evidence_keys: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    project: Mapped[ResearchProject | None] = relationship(back_populates="publications")
