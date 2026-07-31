"""Computer-based testing: question banks, papers, attempts and analytics."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SoftDeleteMixin, SyncMixin, TimestampMixin
from app.models.enums import AttemptStatus, ExamMode, MediaKind, QuestionType

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

    bank: Mapped[QuestionBank] = relationship(back_populates="questions")

    @property
    def facility_index(self) -> float | None:
        """Proportion answering correctly — the classical facility index."""
        if not self.times_served:
            return None
        return self.times_correct / self.times_served


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
    #: Proctoring signals: tab switches, disconnections, offline periods.
    integrity_events: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)

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
