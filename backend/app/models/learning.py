"""Adaptive learning: reading engagement, readiness, integrity and AI generation.

Four subsystems share this module because they share a spine — every one of them
records *observations* about a trainee and derives a number from them, and every
one of those numbers has to remain explainable months later. So each derived row
stores the inputs and the weights it used, not just its result.

The integrity tables deserve a specific note. They record what the browser
reported and nothing else. No row in this module concludes that a trainee
cheated; ``IntegrityReport.outcome`` starts at ``pending_review`` and only a
human can move it. That is a deliberate design constraint, not an oversight —
see ``app.services.integrity``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SoftDeleteMixin, SyncMixin, TimestampMixin
from app.models.enums import (
    AuthoringSource,
    EditorialStatus,
    GenerationStage,
    GenerationTrigger,
    IntegrityOutcome,
    IntegritySeverity,
    ProctoringMode,
    ReadinessCategory,
)

if TYPE_CHECKING:
    from app.models.cbt import ExamAttempt, Question
    from app.models.cme import CmeResource
    from app.models.identity import User


# ==========================================================================
# Reading engagement
# ==========================================================================
class ReadingSession(Base, IdMixin, TimestampMixin, SyncMixin):
    """One continuous sitting with one CME resource.

    A trainee who opens an article four times has four sessions; the *resource*
    level roll-up is a query over them, not a second stored counter that can
    drift out of step.

    ``active_seconds`` counts only heartbeats received while the tab was
    visible. Wall-clock time is deliberately not used: leaving an article open
    over lunch is not reading, and a score that cannot tell the difference is
    trivially gamed.
    """

    __tablename__ = "reading_sessions"
    __table_args__ = (
        Index("ix_reading_sessions_user_resource", "user_id", "resource_id"),
        Index("ix_reading_sessions_tenant_opened", "tenant_id", "opened_at"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_id: Mapped[str] = mapped_column(
        ForeignKey("cme_resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assignment_id: Mapped[str | None] = mapped_column(
        ForeignKey("cme_assignments.id", ondelete="SET NULL"), default=None, index=True
    )

    opened_at: Mapped[datetime] = mapped_column(nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(default=None)
    #: Seconds the tab was visible and the reader was not idle.
    active_seconds: Mapped[int] = mapped_column(default=0, nullable=False)
    #: Deepest scroll position reached, 0-100.
    max_scroll_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Proportion of the article's declared sections marked complete, 0-100.
    section_completion_percent: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    #: Which numbered sections the reader actually reached.
    sections_completed: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    highlight_count: Mapped[int] = mapped_column(default=0, nullable=False)
    note_count: Mapped[int] = mapped_column(default=0, nullable=False)
    bookmark_count: Mapped[int] = mapped_column(default=0, nullable=False)
    download_count: Mapped[int] = mapped_column(default=0, nullable=False)
    reference_follow_count: Mapped[int] = mapped_column(default=0, nullable=False)
    videos_started: Mapped[int] = mapped_column(default=0, nullable=False)
    videos_completed: Mapped[int] = mapped_column(default=0, nullable=False)

    #: True when this is not the reader's first session with the resource.
    is_revisit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Offline reading syncs late; recorded so analytics can say so.
    captured_offline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    resource: Mapped[CmeResource] = relationship()
    user: Mapped[User] = relationship(foreign_keys=[user_id])
    events: Mapped[list[ReadingEvent]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )


class ReadingEvent(Base, IdMixin, TimestampMixin, SyncMixin):
    """Append-only reading telemetry.

    Kept separate from the session roll-up so a disputed engagement score can be
    re-derived from the raw stream rather than argued about.
    """

    __tablename__ = "reading_events"
    __table_args__ = (
        Index("ix_reading_events_session_at", "session_id", "occurred_at"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("reading_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    #: Seconds since the previous event of any kind, as reported by the client.
    delta_seconds: Mapped[int] = mapped_column(default=0, nullable=False)
    scroll_percent: Mapped[float | None] = mapped_column(Float, default=None)
    section_ref: Mapped[str | None] = mapped_column(String(120), default=None)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    session: Mapped[ReadingSession] = relationship(back_populates="events")


class ReadingAnnotation(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    """A highlight, note or bookmark. Belongs to the reader, never shared by default."""

    __tablename__ = "reading_annotations"
    __table_args__ = (
        Index("ix_reading_annotations_user_resource", "user_id", "resource_id"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_id: Mapped[str] = mapped_column(
        ForeignKey("cme_resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("reading_sessions.id", ondelete="SET NULL"), default=None
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # highlight|note|bookmark
    section_ref: Mapped[str | None] = mapped_column(String(120), default=None)
    #: Character offsets into the rendered section, for re-anchoring.
    anchor: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    quoted_text: Mapped[str | None] = mapped_column(Text, default=None)
    body: Mapped[str | None] = mapped_column(Text, default=None)
    colour: Mapped[str | None] = mapped_column(String(16), default=None)
    is_shared_with_supervisor: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )


class EngagementSnapshot(Base, IdMixin, TimestampMixin):
    """The four reading-derived scores, computed over a window and kept.

    Stored rather than computed on demand because the trend *is* the signal —
    a consistency score only means something against the ones before it.
    """

    __tablename__ = "engagement_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "window_start", "window_end", name="uq_engagement_window"
        ),
        Index("ix_engagement_snapshots_tenant_user", "tenant_id", "user_id"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str | None] = mapped_column(
        ForeignKey("enrolments.id", ondelete="SET NULL"), default=None
    )
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)

    #: Volume and completion of assigned reading, 0-100.
    reading_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: How evenly reading is spread across the window rather than crammed, 0-100.
    consistency_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Depth of interaction — highlights, notes, references followed, 0-100.
    engagement_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Performance on items covering material read earlier, 0-100.
    retention_score: Mapped[float | None] = mapped_column(Float, default=None)

    articles_opened: Mapped[int] = mapped_column(default=0, nullable=False)
    articles_completed: Mapped[int] = mapped_column(default=0, nullable=False)
    active_minutes: Mapped[int] = mapped_column(default=0, nullable=False)
    distinct_active_days: Mapped[int] = mapped_column(default=0, nullable=False)
    #: Every input and weight used, so the number can be explained later.
    components: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)


# ==========================================================================
# Examination readiness
# ==========================================================================
class ReadinessSnapshot(Base, IdMixin, TimestampMixin):
    """The Examination Readiness Score at a point in time.

    ``components`` holds one entry per weighted contributor with its raw score,
    weight, whether it was assessed at all, and how much evidence stood behind
    it. ``confidence_low``/``confidence_high`` are not decoration: a trainee
    with two logbook entries and one CBT sitting has a genuinely uncertain
    score, and presenting it as a point estimate would be dishonest.
    """

    __tablename__ = "readiness_snapshots"
    __table_args__ = (
        Index("ix_readiness_snapshots_user_date", "user_id", "as_of"),
        Index("ix_readiness_snapshots_tenant_cat", "tenant_id", "category"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str | None] = mapped_column(
        ForeignKey("enrolments.id", ondelete="SET NULL"), default=None, index=True
    )
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    #: Which examination this predicts readiness for, when one is named.
    target_examination: Mapped[str | None] = mapped_column(String(160), default=None)
    target_date: Mapped[date | None] = mapped_column(Date, default=None)

    score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    category: Mapped[str] = mapped_column(
        String(32), default=ReadinessCategory.NEEDS_IMPROVEMENT, nullable=False
    )
    confidence_low: Mapped[float] = mapped_column(Float, nullable=False)
    confidence_high: Mapped[float] = mapped_column(Float, nullable=False)
    #: 0-1. How much of the weighted base had real evidence behind it.
    evidence_coverage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    #: {"cbt_performance": {"score": 71.2, "weight": 0.35, "assessed": true, ...}}
    components: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: Ranked; each entry carries the score change a fixed improvement would buy.
    influential_factors: Mapped[list[dict[str, Any]]] = mapped_column(
        default=list, nullable=False
    )
    #: The nine dashboard indices, computed alongside and kept together.
    indices: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: The weight table in force when this was computed.
    weights_used: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: Points change against the previous snapshot, for trend display.
    delta_from_previous: Mapped[float | None] = mapped_column(Float, default=None)

    user: Mapped[User] = relationship(foreign_keys=[user_id])


# ==========================================================================
# Remediation
# ==========================================================================
class LearningPlan(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    """An individual learning plan generated from measured weakness."""

    __tablename__ = "learning_plans"
    __table_args__ = (Index("ix_learning_plans_user_status", "user_id", "status"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str | None] = mapped_column(
        ForeignKey("enrolments.id", ondelete="SET NULL"), default=None
    )
    readiness_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("readiness_snapshots.id", ondelete="SET NULL"), default=None
    )
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, default=None)
    #: manual | readiness | exam_result | supervisor
    origin: Mapped[str] = mapped_column(String(32), default="readiness", nullable=False)
    authoring_source: Mapped[str] = mapped_column(
        String(24), default=AuthoringSource.HUMAN, nullable=False
    )
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    #: Weak areas the plan addresses, each with the evidence that identified it.
    target_areas: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    #: A supervisor may have to sign a remediation plan off before it counts.
    approved_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    approved_at: Mapped[datetime | None] = mapped_column(default=None)
    completion_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    actions: Mapped[list[LearningPlanAction]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", passive_deletes=True
    )


class LearningPlanAction(Base, IdMixin, TimestampMixin, SyncMixin):
    __tablename__ = "learning_plan_actions"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("learning_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(default=0, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    #: The competency, topic or procedure this action is meant to move.
    target_ref: Mapped[str | None] = mapped_column(String(160), default=None)
    #: Resource / paper / activity this action points at, when it points at one.
    resource_id: Mapped[str | None] = mapped_column(
        ForeignKey("cme_resources.id", ondelete="SET NULL"), default=None
    )
    paper_id: Mapped[str | None] = mapped_column(
        ForeignKey("exam_papers.id", ondelete="SET NULL"), default=None
    )
    estimated_minutes: Mapped[int] = mapped_column(default=30, nullable=False)
    due_on: Mapped[date | None] = mapped_column(Date, default=None)
    #: pending | in_progress | done | skipped
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)
    evidence_ref: Mapped[str | None] = mapped_column(String(64), default=None)

    plan: Mapped[LearningPlan] = relationship(back_populates="actions")


# ==========================================================================
# Examination integrity
# ==========================================================================
class IntegrityPolicy(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """Per-institution examination conduct settings.

    Every anti-cheating measure the specification lists is a column here rather
    than a constant in code, because the specification marks the whole set
    "institution configurable" — and because a formative practice quiz and a
    fellowship mock exam should not be policed identically.
    """

    __tablename__ = "integrity_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_integrity_policy_code"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="SET NULL"), default=None, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)

    require_fullscreen: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    log_focus_changes: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    log_clipboard_attempts: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    block_copy_paste: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    block_printing: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    block_context_menu: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    single_session_only: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    randomise_question_order: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    randomise_option_order: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    #: Draw from a pool this many times the paper length, so no two sittings match.
    pool_multiplier: Mapped[float] = mapped_column(Float, default=3.0, nullable=False)
    #: Minutes of inactivity before the session is treated as abandoned.
    idle_timeout_minutes: Mapped[int] = mapped_column(default=20, nullable=False)
    auto_submit_on_expiry: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    #: Device fingerprinting stores a salted hash only — never a raw identifier.
    device_fingerprinting: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    ip_anomaly_detection: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    proctoring_mode: Mapped[str] = mapped_column(
        String(24), default=ProctoringMode.EVENT_LOGGING, nullable=False
    )
    #: Camera and microphone are only ever enabled behind explicit consent.
    require_explicit_consent: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    consent_statement: Mapped[str | None] = mapped_column(Text, default=None)
    #: Days after which raw integrity telemetry is purged. NDPR data minimisation.
    retain_events_days: Mapped[int] = mapped_column(default=180, nullable=False)

    #: Thresholds above which the report is flagged for a human to look at.
    focus_loss_notice_threshold: Mapped[int] = mapped_column(default=3, nullable=False)
    focus_loss_concern_threshold: Mapped[int] = mapped_column(default=8, nullable=False)
    #: Answers faster than this are noted; they are not proof of anything.
    rapid_response_seconds: Mapped[int] = mapped_column(default=3, nullable=False)

    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class IntegrityEvent(Base, IdMixin, TimestampMixin, SyncMixin):
    """One observation reported by the exam client. Append-only.

    ``severity`` is assigned by policy thresholds, not by judgement, and no
    value of it means misconduct.
    """

    __tablename__ = "integrity_events"
    __table_args__ = (
        Index("ix_integrity_events_attempt_at", "attempt_id", "occurred_at"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("exam_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(
        String(16), default=IntegritySeverity.INFO, nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    #: Seconds the condition persisted, where that is meaningful (e.g. tab hidden).
    duration_seconds: Mapped[int] = mapped_column(default=0, nullable=False)
    question_sequence: Mapped[int | None] = mapped_column(default=None)
    #: Salted hash. The raw fingerprint and raw IP are never stored.
    device_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    network_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    attempt: Mapped[ExamAttempt] = relationship()


class IntegrityReport(Base, IdMixin, TimestampMixin):
    """The post-examination integrity summary for one attempt.

    Exactly one per attempt. Produced automatically on submission; its
    ``outcome`` begins at ``pending_review`` when anything was flagged and can
    only be advanced by a named human, whose identity and reasoning are stored.
    """

    __tablename__ = "integrity_reports"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_integrity_report_attempt"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("exam_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_id: Mapped[str | None] = mapped_column(
        ForeignKey("integrity_policies.id", ondelete="SET NULL"), default=None
    )
    generated_at: Mapped[datetime] = mapped_column(nullable=False)

    event_count: Mapped[int] = mapped_column(default=0, nullable=False)
    #: {"window_blurred": 4, "tab_hidden": 2, ...}
    event_counts: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    focus_loss_count: Mapped[int] = mapped_column(default=0, nullable=False)
    focus_loss_seconds: Mapped[int] = mapped_column(default=0, nullable=False)
    clipboard_attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    distinct_devices: Mapped[int] = mapped_column(default=0, nullable=False)
    distinct_networks: Mapped[int] = mapped_column(default=0, nullable=False)
    rapid_responses: Mapped[int] = mapped_column(default=0, nullable=False)
    was_auto_submitted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: Plain-language observations, each naming the policy threshold it crossed.
    observations: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    #: Advisory only. Never a penalty, never surfaced to the candidate as a verdict.
    requires_human_review: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    outcome: Mapped[str] = mapped_column(
        String(32), default=IntegrityOutcome.CLEAN, nullable=False, index=True
    )
    reviewed_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(default=None)
    review_notes: Mapped[str | None] = mapped_column(Text, default=None)
    #: The candidate's own account, when they are invited to give one.
    candidate_statement: Mapped[str | None] = mapped_column(Text, default=None)


class ExamConsent(Base, IdMixin, TimestampMixin):
    """Recorded consent for optional proctoring.

    Absence of a row means no consent. Nothing in the codebase enables camera or
    microphone capture without a matching row, and consent can be withdrawn.
    """

    __tablename__ = "exam_consents"
    __table_args__ = (
        UniqueConstraint("attempt_id", "user_id", name="uq_exam_consent_attempt"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("exam_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_id: Mapped[str | None] = mapped_column(
        ForeignKey("integrity_policies.id", ondelete="SET NULL"), default=None
    )
    #: The exact wording the candidate agreed to, copied at the time.
    statement_shown: Mapped[str] = mapped_column(Text, nullable=False)
    camera_granted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    microphone_granted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(nullable=False)
    withdrawn_at: Mapped[datetime | None] = mapped_column(default=None)

    @property
    def is_current(self) -> bool:
        return self.withdrawn_at is None


# ==========================================================================
# AI generation
# ==========================================================================
class GenerationJob(Base, IdMixin, TimestampMixin):
    """One run of the weekly-CBT generation workflow.

    The job is the audit trail. It records what was asked for, which knowledge
    was retrieved, what each pipeline stage rejected and why, what it cost, and
    who released it — so a paper that turns out to contain a bad item can be
    traced back to the decision that let it through.
    """

    __tablename__ = "generation_jobs"
    __table_args__ = (
        Index("ix_generation_jobs_tenant_stage", "tenant_id", "stage"),
        Index("ix_generation_jobs_requested", "tenant_id", "requested_at"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="SET NULL"), default=None, index=True
    )
    bank_id: Mapped[str | None] = mapped_column(
        ForeignKey("question_banks.id", ondelete="SET NULL"), default=None, index=True
    )
    specialty_id: Mapped[str | None] = mapped_column(
        ForeignKey("specialties.id", ondelete="SET NULL"), default=None
    )
    requested_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    #: Set when the paper is personalised to one trainee.
    target_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), default=None, index=True
    )

    trigger: Mapped[str] = mapped_column(
        String(24), default=GenerationTrigger.MANUAL, nullable=False
    )
    stage: Mapped[str] = mapped_column(
        String(32), default=GenerationStage.QUEUED, nullable=False, index=True
    )
    training_level: Mapped[str | None] = mapped_column(String(32), default=None)
    topics: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    learning_objectives: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    requested_count: Mapped[int] = mapped_column(default=50, nullable=False)
    #: The category blueprint in force for this run, as proportions summing to 1.
    blueprint: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: The difficulty mix requested, as proportions summing to 1.
    difficulty_mix: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    requested_at: Mapped[datetime] = mapped_column(nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    #: The service-level target. Breaches are recorded, not hidden.
    deadline_minutes: Mapped[int] = mapped_column(default=20, nullable=False)

    #: Per-stage timings and rejection counts, keyed by GenerationStage.
    stage_log: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    #: The sources the generator was given, ranked.
    knowledge_sources: Mapped[list[dict[str, Any]]] = mapped_column(
        default=list, nullable=False
    )
    generated_count: Mapped[int] = mapped_column(default=0, nullable=False)
    accepted_count: Mapped[int] = mapped_column(default=0, nullable=False)
    rejected_count: Mapped[int] = mapped_column(default=0, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(default=0, nullable=False)
    regeneration_rounds: Mapped[int] = mapped_column(default=0, nullable=False)

    provider: Mapped[str | None] = mapped_column(String(48), default=None)
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    input_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    #: Whether a consultant must approve before trainees can sit the paper.
    requires_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: ``use_alter`` breaks a genuine cycle: a paper records the job that made
    #: it, and a job records the paper it released. Without this SQLAlchemy
    #: cannot order CREATE/DROP and warns that it will ignore the constraints
    #: entirely. Naming it explicitly is required too — Alembic cannot emit a
    #: separate ALTER for an unnamed constraint.
    released_paper_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "exam_papers.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_generation_jobs_released_paper_id",
        ),
        default=None,
    )
    released_at: Mapped[datetime | None] = mapped_column(default=None)
    released_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    error: Mapped[str | None] = mapped_column(Text, default=None)

    drafts: Mapped[list[QuestionDraft]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def elapsed_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or self.started_at
        return (end - self.started_at).total_seconds()

    @property
    def met_deadline(self) -> bool | None:
        elapsed = self.elapsed_seconds
        if elapsed is None or self.finished_at is None:
            return None
        return elapsed <= self.deadline_minutes * 60


class QuestionDraft(Base, IdMixin, TimestampMixin):
    """A generated item before it becomes a ``Question``.

    Rejected drafts are kept. They are the only way to answer "why did we only
    get 43 usable items out of 60?" and the raw material for improving prompts.
    """

    __tablename__ = "question_drafts"
    __table_args__ = (
        Index("ix_question_drafts_job_accepted", "job_id", "is_accepted"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(default=0, nullable=False)

    question_type: Mapped[str] = mapped_column(String(32), nullable=False)
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    lead_in: Mapped[str | None] = mapped_column(Text, default=None)
    options: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    correct_keys: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text, default=None)
    references: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    topic: Mapped[str | None] = mapped_column(String(160), default=None)
    subtopic: Mapped[str | None] = mapped_column(String(160), default=None)
    blueprint_category: Mapped[str | None] = mapped_column(String(64), default=None)
    difficulty: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    difficulty_band: Mapped[str | None] = mapped_column(String(24), default=None)
    bloom_level: Mapped[str | None] = mapped_column(String(24), default=None)
    competency_domain: Mapped[str | None] = mapped_column(String(40), default=None)
    competency_codes: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    learning_objectives: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    #: The generator's own confidence, 0-1. Advisory input to review triage only.
    ai_confidence: Mapped[float | None] = mapped_column(Float, default=None)

    #: Normalised stem hash, for exact-duplicate detection.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Token shingles, for near-duplicate detection against the live bank.
    shingles: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    is_accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: [{"check": "exactly_one_correct", "passed": false, "detail": "..."}]
    check_results: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, default=None)
    duplicate_of_id: Mapped[str | None] = mapped_column(
        ForeignKey("questions.id", ondelete="SET NULL"), default=None
    )
    #: Set once promoted into the live bank.
    promoted_question_id: Mapped[str | None] = mapped_column(
        ForeignKey("questions.id", ondelete="SET NULL"), default=None
    )

    job: Mapped[GenerationJob] = relationship(back_populates="drafts")


class QuestionVersion(Base, IdMixin, TimestampMixin):
    """An immutable snapshot of an item at one point in its editorial life.

    Written on every content change and every status transition. A question that
    was answered by 400 trainees last year must still be readable in the form
    they saw, or their scores cannot be defended.
    """

    __tablename__ = "question_versions"
    __table_args__ = (
        UniqueConstraint("question_id", "version", name="uq_question_version"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[str] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(nullable=False)
    editorial_status: Mapped[str] = mapped_column(
        String(24), default=EditorialStatus.DRAFT, nullable=False
    )
    #: The full item body as it stood — stem, options, explanation, references.
    snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text, default=None)
    changed_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    question: Mapped[Question] = relationship()


class QuestionReview(Base, IdMixin, TimestampMixin):
    """A named human's decision on an item. The publication gate's audit trail."""

    __tablename__ = "question_reviews"
    __table_args__ = (
        Index("ix_question_reviews_question", "question_id", "created_at"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[str] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reviewer_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: approve | request_changes | reject | retire
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    from_status: Mapped[str] = mapped_column(String(24), nullable=False)
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    comments: Mapped[str | None] = mapped_column(Text, default=None)
    #: Structured scoring of the item, when the institution asks reviewers for it.
    scores: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    version_reviewed: Mapped[int | None] = mapped_column(default=None)


class ItemAnalysis(Base, IdMixin, TimestampMixin):
    """Psychometrics for one item against one paper's cohort.

    Kept per paper rather than as a single rolling figure on the question,
    because a facility index only means something relative to the cohort that
    produced it. ``Question.difficulty`` remains the blended live estimate.
    """

    __tablename__ = "item_analyses"
    __table_args__ = (
        UniqueConstraint("paper_id", "question_id", name="uq_item_analysis"),
        Index("ix_item_analyses_tenant_paper", "tenant_id", "paper_id"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("exam_papers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[str] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    computed_at: Mapped[datetime] = mapped_column(nullable=False)
    candidates: Mapped[int] = mapped_column(default=0, nullable=False)
    #: Proportion correct.
    facility: Mapped[float | None] = mapped_column(Float, default=None)
    #: Point-biserial correlation with total score.
    discrimination: Mapped[float | None] = mapped_column(Float, default=None)
    #: Upper-third minus lower-third facility.
    discrimination_index: Mapped[float | None] = mapped_column(Float, default=None)
    mean_seconds: Mapped[float | None] = mapped_column(Float, default=None)
    #: {"A": {"count": 12, "share": 0.24, "is_key": false, "upper": 2, "lower": 8}}
    distractor_stats: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: Distractors chosen by nobody, or chosen more by strong candidates.
    flags: Mapped[list[str]] = mapped_column(default=list, nullable=False)


class PaperAnalysis(Base, IdMixin, TimestampMixin):
    """Whole-paper reliability statistics for one cohort."""

    __tablename__ = "paper_analyses"
    __table_args__ = (
        Index("ix_paper_analyses_paper_at", "paper_id", "computed_at"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("exam_papers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    computed_at: Mapped[datetime] = mapped_column(nullable=False)
    candidates: Mapped[int] = mapped_column(default=0, nullable=False)
    items: Mapped[int] = mapped_column(default=0, nullable=False)
    mean_percent: Mapped[float | None] = mapped_column(Float, default=None)
    sd_percent: Mapped[float | None] = mapped_column(Float, default=None)
    median_percent: Mapped[float | None] = mapped_column(Float, default=None)
    pass_rate: Mapped[float | None] = mapped_column(Float, default=None)
    #: Kuder-Richardson 20 — the dichotomous case.
    kr20: Mapped[float | None] = mapped_column(Float, default=None)
    #: Cronbach's alpha — identical to KR-20 for dichotomous items, kept for
    #: partially-credited papers where they diverge.
    cronbach_alpha: Mapped[float | None] = mapped_column(Float, default=None)
    #: Standard error of measurement, in percentage points.
    sem: Mapped[float | None] = mapped_column(Float, default=None)
    mean_facility: Mapped[float | None] = mapped_column(Float, default=None)
    mean_discrimination: Mapped[float | None] = mapped_column(Float, default=None)
    #: Requested against delivered proportions, per blueprint category.
    blueprint_coverage: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: Items whose statistics warrant review, with the reason.
    flagged_items: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
