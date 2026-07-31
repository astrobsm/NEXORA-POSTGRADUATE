"""Computer-based testing: question banks, papers, attempts and analytics."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    Base,
    IdMixin,
    JsonList,
    SoftDeleteMixin,
    SyncMixin,
    TimestampMixin,
)
from app.models.enums import (
    AttemptStatus,
    AuthoringSource,
    EditorialStatus,
    ExamMode,
    IntegrityOutcome,
    MediaKind,
    QuestionType,
)

if TYPE_CHECKING:
    from app.models.identity import User


class QuestionBank(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "question_banks"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_question_bank_code"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="SET NULL"), default=None, index=True
    )
    specialty_id: Mapped[str | None] = mapped_column(
        ForeignKey("specialties.id", ondelete="SET NULL"), default=None
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    #: Restrict visibility to specific training levels.
    applies_to_levels: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    questions: Mapped[list[Question]] = relationship(
        back_populates="bank", cascade="all, delete-orphan", passive_deletes=True
    )


class Question(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    """A single item. Supports SBA, EMQ, true/false sets, image/video and OSCE stations."""

    __tablename__ = "questions"
    __table_args__ = (
        Index("ix_questions_bank_active", "bank_id", "is_active"),
        Index("ix_questions_difficulty", "tenant_id", "difficulty"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bank_id: Mapped[str] = mapped_column(
        ForeignKey("question_banks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_type: Mapped[str] = mapped_column(
        String(32), default=QuestionType.SINGLE_BEST_ANSWER, nullable=False
    )
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    lead_in: Mapped[str | None] = mapped_column(Text, default=None)
    #: [{"key": "A", "text": "...", "is_correct": true, "rationale": "..."}, ...]
    options: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    #: For EMQ: the shared option list is stored on the parent set.
    parent_set_id: Mapped[str | None] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), default=None
    )
    correct_keys: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text, default=None)
    references: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    media_kind: Mapped[str] = mapped_column(String(24), default=MediaKind.NONE, nullable=False)
    media_keys: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    #: 0.0 (trivial) — 1.0 (very hard). Updated from live attempt statistics.
    difficulty: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    #: Item discrimination index, recomputed periodically.
    discrimination: Mapped[float | None] = mapped_column(Float, default=None)
    times_served: Mapped[int] = mapped_column(default=0, nullable=False)
    times_correct: Mapped[int] = mapped_column(default=0, nullable=False)

    topic: Mapped[str | None] = mapped_column(String(160), default=None, index=True)
    subtopic: Mapped[str | None] = mapped_column(String(160), default=None)
    tags: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    competency_codes: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    default_seconds: Mapped[int] = mapped_column(default=90, nullable=False)
    marks: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    author_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    reviewed_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ---- Provenance and the publication gate -----------------------------
    #: How this item came to exist. Recorded, never inferred.
    authoring_source: Mapped[str] = mapped_column(
        String(24), default=AuthoringSource.HUMAN, nullable=False, index=True
    )
    #: Trainees only ever see PUBLISHED items. AI output enters at AI_DRAFT.
    editorial_status: Mapped[str] = mapped_column(
        String(24), default=EditorialStatus.DRAFT, nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), default=None, index=True
    )
    #: The generator's confidence, 0-1. Used to triage review, never to publish.
    ai_confidence: Mapped[float | None] = mapped_column(Float, default=None)
    published_at: Mapped[datetime | None] = mapped_column(default=None)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(default=None, index=True)
    retired_at: Mapped[datetime | None] = mapped_column(default=None)

    # ---- Curriculum and blueprint mapping --------------------------------
    difficulty_band: Mapped[str | None] = mapped_column(String(24), default=None, index=True)
    bloom_level: Mapped[str | None] = mapped_column(String(24), default=None)
    competency_domain: Mapped[str | None] = mapped_column(String(40), default=None)
    blueprint_category: Mapped[str | None] = mapped_column(
        String(64), default=None, index=True
    )
    learning_objectives: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    evidence_level: Mapped[str | None] = mapped_column(String(24), default=None)
    publication_year: Mapped[int | None] = mapped_column(default=None)
    #: The CME article that teaches this item, offered with post-exam feedback.
    cme_resource_id: Mapped[str | None] = mapped_column(
        ForeignKey("cme_resources.id", ondelete="SET NULL"), default=None
    )

    # ---- Rotation and duplicate control ----------------------------------
    #: Normalised stem hash. Unique-ish by construction; the index is not a
    #: constraint because two legitimately different items can share a stem
    #: opening, and blocking that at the database would be wrong.
    content_hash: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    shingles: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    #: When this item was last served to anyone, for pool rotation.
    last_served_at: Mapped[datetime | None] = mapped_column(default=None, index=True)
    #: Live distractor shares, blended across cohorts.
    distractor_stats: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    bank: Mapped[QuestionBank] = relationship(back_populates="questions")

    @property
    def facility_index(self) -> float | None:
        """Proportion answering correctly — the classical facility index."""
        if not self.times_served:
            return None
        return self.times_correct / self.times_served

    @property
    def is_servable(self) -> bool:
        """Whether this item may be put in front of a trainee.

        Active is not enough. An AI-generated item that no one has reviewed is
        active and complete and still must not be served.
        """
        return (
            self.is_active
            and self.deleted_at is None
            and self.editorial_status == EditorialStatus.PUBLISHED
        )


class ExamPaper(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """A schedulable paper — fixed items or a blueprint drawn at attempt time."""

    __tablename__ = "exam_papers"
    __table_args__ = (Index("ix_exam_papers_tenant_mode", "tenant_id", "mode"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="SET NULL"), default=None, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    mode: Mapped[str] = mapped_column(String(24), default=ExamMode.PRACTICE, nullable=False)
    #: Blueprint: [{"bank_id": "...", "topic": "cardiology", "count": 20,
    #:              "difficulty_range": [0.3, 0.8]}]
    blueprint: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    #: Explicit question ids when the paper is fixed.
    question_ids: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    question_count: Mapped[int] = mapped_column(default=50, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(default=90, nullable=False)
    pass_mark_percent: Mapped[float] = mapped_column(Float, default=50.0, nullable=False)
    shuffle_questions: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    shuffle_options: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    show_feedback: Mapped[str] = mapped_column(String(24), default="after_submit", nullable=False)
    max_attempts: Mapped[int | None] = mapped_column(default=None)
    opens_at: Mapped[datetime | None] = mapped_column(default=None)
    closes_at: Mapped[datetime | None] = mapped_column(default=None)
    applies_to_levels: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ---- Conduct, adaptivity and provenance ------------------------------
    #: Which conduct policy governs sittings of this paper. Null means the
    #: institution default, which is itself a row rather than a constant.
    integrity_policy_id: Mapped[str | None] = mapped_column(
        ForeignKey("integrity_policies.id", ondelete="SET NULL"), default=None
    )
    #: Proportions per blueprint category, e.g. {"basic_sciences": 0.10, ...}.
    blueprint_profile: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: Proportions per difficulty band, e.g. {"easy": 0.20, "moderate": 0.40, ...}.
    difficulty_mix: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: When true the next item is chosen from the candidate's running accuracy
    #: rather than fixed at assembly time.
    adaptive_difficulty: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    #: A personalised weekly paper belongs to one trainee.
    target_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), default=None, index=True
    )
    generated_by_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), default=None, index=True
    )
    #: Weekly papers are issued for a specific week; the pair is what makes
    #: "have they sat this week's CBT?" answerable without date arithmetic.
    cycle_year: Mapped[int | None] = mapped_column(default=None)
    cycle_week: Mapped[int | None] = mapped_column(default=None)

    attempts: Mapped[list[ExamAttempt]] = relationship(
        back_populates="paper", passive_deletes=True
    )


class ExamAttempt(Base, IdMixin, TimestampMixin, SyncMixin):
    __tablename__ = "exam_attempts"
    __table_args__ = (
        Index("ix_exam_attempts_user_paper", "user_id", "paper_id"),
        Index("ix_exam_attempts_status", "tenant_id", "status"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("exam_papers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrolment_id: Mapped[str | None] = mapped_column(
        ForeignKey("enrolments.id", ondelete="SET NULL"), default=None, index=True
    )
    attempt_number: Mapped[int] = mapped_column(default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=AttemptStatus.IN_PROGRESS, nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(default=None)
    seconds_used: Mapped[int] = mapped_column(default=0, nullable=False)

    #: The materialised question order for this attempt (blueprint resolved).
    served_question_ids: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    total_marks: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    scored_marks: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    percent_score: Mapped[float | None] = mapped_column(Float, default=None, index=True)
    is_pass: Mapped[bool | None] = mapped_column(Boolean, default=None)
    #: {"cardiology": {"served": 10, "correct": 7}, ...}
    topic_breakdown: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: Percentile against the cohort sitting the same paper.
    cohort_percentile: Mapped[float | None] = mapped_column(Float, default=None)
    #: Proctoring signals captured inline by an offline client. The durable
    #: record is ``IntegrityEvent``; this is the sync landing area, drained on
    #: submission so an offline sitting loses nothing.
    integrity_events: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)

    # ---- Conduct ---------------------------------------------------------
    #: Signed at issue, verified on every answer. A resumed sitting must present
    #: the same token, which is what makes "one session per candidate" real
    #: rather than advisory.
    session_token: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    #: Salted hashes only. The raw fingerprint and raw IP are never stored.
    device_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    network_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    #: Set by the integrity report. Only a human may advance it past review.
    integrity_outcome: Mapped[str] = mapped_column(
        String(32), default=IntegrityOutcome.CLEAN, nullable=False
    )
    #: Per-item difficulty actually served, when the paper is adaptive.
    #: Annotated explicitly: ``list[float]`` is not in ``Base.type_annotation_map``
    #: and SQLAlchemy would otherwise refuse to resolve a column type for it.
    served_difficulties: Mapped[list[float]] = mapped_column(
        JsonList, default=list, nullable=False
    )
    was_auto_submitted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    paper: Mapped[ExamPaper] = relationship(back_populates="attempts")
    user: Mapped[User] = relationship(foreign_keys=[user_id])
    responses: Mapped[list[ExamResponse]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan", passive_deletes=True
    )


class ExamResponse(Base, IdMixin, TimestampMixin, SyncMixin):
    __tablename__ = "exam_responses"
    __table_args__ = (
        UniqueConstraint("attempt_id", "question_id", name="uq_exam_response"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("exam_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[str] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(default=0, nullable=False)
    selected_keys: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    free_text: Mapped[str | None] = mapped_column(Text, default=None)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, default=None)
    marks_awarded: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    seconds_spent: Mapped[int] = mapped_column(default=0, nullable=False)
    flagged_for_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confidence: Mapped[str | None] = mapped_column(String(16), default=None)
    #: Cached AI-generated explanation for this candidate's specific error.
    ai_feedback: Mapped[str | None] = mapped_column(Text, default=None)

    attempt: Mapped[ExamAttempt] = relationship(back_populates="responses")
    question: Mapped[Question] = relationship()
