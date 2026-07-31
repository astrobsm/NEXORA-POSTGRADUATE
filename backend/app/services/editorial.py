"""The publication gate for questions and CME articles.

One rule, enforced in one place: **AI-generated material is identified as such
until a human reviews it, and cannot reach a trainee before then.**

The mechanism is a small state machine. Content enters at ``draft`` (human) or
``ai_draft`` (generated), moves through review, and only a transition performed
by a named user holding the review permission reaches ``published``. Every
transition writes a :class:`QuestionReview` row and an immutable
:class:`QuestionVersion` snapshot, so the question of who approved what, and
what it said at the time, always has an answer.

The transition table is deliberately restrictive. There is no edge from
``ai_draft`` to ``published``: generated content must pass through ``in_review``
and ``approved``, which means at least one person has opened it. Allowing a bulk
"approve all" from draft would technically satisfy the requirement and defeat
its purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.cbt import Question
from app.models.cme import CmeResource
from app.models.enums import AuthoringSource, EditorialStatus
from app.models.learning import QuestionReview, QuestionVersion

#: Permitted transitions. Anything not listed is refused with an explanation.
TRANSITIONS: dict[str, set[str]] = {
    EditorialStatus.DRAFT: {
        EditorialStatus.IN_REVIEW,
        EditorialStatus.REJECTED,
    },
    EditorialStatus.AI_DRAFT: {
        # Note the absence of PUBLISHED. Generated content cannot skip review.
        EditorialStatus.IN_REVIEW,
        EditorialStatus.REJECTED,
    },
    EditorialStatus.IN_REVIEW: {
        EditorialStatus.APPROVED,
        EditorialStatus.CHANGES_REQUESTED,
        EditorialStatus.REJECTED,
    },
    EditorialStatus.CHANGES_REQUESTED: {
        EditorialStatus.IN_REVIEW,
        EditorialStatus.REJECTED,
    },
    EditorialStatus.APPROVED: {
        EditorialStatus.PUBLISHED,
        EditorialStatus.CHANGES_REQUESTED,
    },
    EditorialStatus.PUBLISHED: {
        EditorialStatus.RETIRED,
        EditorialStatus.CHANGES_REQUESTED,
    },
    EditorialStatus.RETIRED: {EditorialStatus.IN_REVIEW},
    EditorialStatus.REJECTED: {EditorialStatus.DRAFT},
}

#: Decisions a reviewer can record, and the status each moves an item to.
DECISIONS: dict[str, str] = {
    "submit": EditorialStatus.IN_REVIEW,
    "approve": EditorialStatus.APPROVED,
    "publish": EditorialStatus.PUBLISHED,
    "request_changes": EditorialStatus.CHANGES_REQUESTED,
    "reject": EditorialStatus.REJECTED,
    "retire": EditorialStatus.RETIRED,
}

#: Decisions that require a comment. Refusing or asking for changes without
#: saying why leaves the author with nothing to act on.
COMMENT_REQUIRED = {"request_changes", "reject", "retire"}


class EditorialError(RuntimeError):
    """A transition is not permitted, or is missing something it requires."""


@dataclass(slots=True)
class ReviewQueueEntry:
    question_id: str
    stem: str
    topic: str | None
    blueprint_category: str | None
    difficulty_band: str | None
    editorial_status: str
    authoring_source: str
    ai_confidence: float | None
    generation_job_id: str | None
    version: int
    #: Advisory style notes from the quality pass. Never a reason to reject on
    #: their own — they exist to point a reviewer's eye somewhere useful.
    advisory_notes: list[str]
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "stem": self.stem,
            "topic": self.topic,
            "blueprint_category": self.blueprint_category,
            "difficulty_band": self.difficulty_band,
            "editorial_status": self.editorial_status,
            "authoring_source": self.authoring_source,
            "is_ai_generated": self.authoring_source
            in (AuthoringSource.AI_GENERATED, AuthoringSource.AI_ASSISTED),
            "ai_confidence": self.ai_confidence,
            "generation_job_id": self.generation_job_id,
            "version": self.version,
            "advisory_notes": self.advisory_notes,
            "created_at": self.created_at.isoformat(),
        }


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, set())


def _assert_transition(current: str, target: str) -> None:
    if current == target:
        raise EditorialError(f"The item is already '{current}'.")
    if not can_transition(current, target):
        allowed = ", ".join(sorted(TRANSITIONS.get(current, set()))) or "nothing"
        raise EditorialError(
            f"'{current}' cannot move to '{target}'. Permitted from here: {allowed}."
        )


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------
def review_question(
    db: Session,
    question: Question,
    *,
    reviewer_id: str,
    decision: str,
    comments: str | None = None,
    scores: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> QuestionReview:
    """Record a reviewer's decision and move the item.

    ``reviewer_id`` is mandatory and stored. The API layer is responsible for
    checking that the user holds ``cbt.question.review``; this function is
    responsible for making sure whoever it was is recorded, because an approval
    with no name against it is not an approval.
    """
    now = now or utcnow()
    if decision not in DECISIONS:
        raise EditorialError(
            f"'{decision}' is not a review decision. "
            f"Expected one of: {', '.join(sorted(DECISIONS))}."
        )
    if decision in COMMENT_REQUIRED and not (comments or "").strip():
        raise EditorialError(
            f"A '{decision}' decision must say why, so the author can act on it."
        )

    target = DECISIONS[decision]
    current = question.editorial_status
    _assert_transition(current, target)

    if decision == "publish" and question.authoring_source in (
        AuthoringSource.AI_GENERATED,
        AuthoringSource.AI_ASSISTED,
    ):
        # Belt and braces alongside the transition table: an AI item can only
        # be published from `approved`, which can only be reached from
        # `in_review`. If a future edit to the table loosened that, this stops
        # the specific case the requirement exists for.
        approvals = db.execute(
            select(func.count())
            .select_from(QuestionReview)
            .where(
                QuestionReview.question_id == question.id,
                QuestionReview.decision == "approve",
            )
        ).scalar_one()
        if not approvals:
            raise EditorialError(
                "AI-generated items must be approved by a reviewer before they "
                "can be published."
            )

    question.editorial_status = target
    question.last_reviewed_at = now
    question.reviewed_by_id = reviewer_id
    if target == EditorialStatus.PUBLISHED:
        question.published_at = now
    if target == EditorialStatus.RETIRED:
        question.retired_at = now
        question.is_active = False

    review = QuestionReview(
        tenant_id=question.tenant_id,
        question_id=question.id,
        reviewer_id=reviewer_id,
        decision=decision,
        from_status=current,
        to_status=target,
        comments=(comments or "").strip() or None,
        scores=scores or {},
        version_reviewed=question.version,
    )
    db.add(review)
    _snapshot(db, question, change_summary=f"{decision}: {current} -> {target}",
              changed_by_id=reviewer_id)
    db.flush()
    return review


def edit_question(
    db: Session,
    question: Question,
    *,
    editor_id: str,
    changes: dict[str, Any],
    change_summary: str,
    now: datetime | None = None,
) -> Question:
    """Apply an edit, bump the version, and snapshot the result.

    Editing a published item sends it back to ``changes_requested``. That is
    intentional and occasionally unpopular: a live item that has been altered
    has not been reviewed in its current form, and candidates sitting it
    tomorrow deserve the same guarantee as candidates who sat it last week.

    A human editing AI-generated content promotes it to ``ai_assisted``, which
    is the honest description — the item is no longer purely machine output,
    and nor is it purely human.
    """
    now = now or utcnow()
    editable = {
        "stem",
        "lead_in",
        "options",
        "correct_keys",
        "explanation",
        "references",
        "topic",
        "subtopic",
        "blueprint_category",
        "difficulty_band",
        "bloom_level",
        "competency_domain",
        "learning_objectives",
        "marks",
        "default_seconds",
        "media_kind",
        "media_keys",
        "cme_resource_id",
    }
    unknown = set(changes) - editable
    if unknown:
        raise EditorialError(
            f"These fields cannot be edited here: {', '.join(sorted(unknown))}."
        )

    for field_name, value in changes.items():
        setattr(question, field_name, value)

    if "stem" in changes or "options" in changes:
        # The fingerprints are what duplicate detection compares against; an
        # edited stem with a stale hash would let its own duplicate through.
        from app.services.ai.quality import content_hash, shingles

        question.content_hash = content_hash(question.stem, question.options)
        question.shingles = shingles(question.stem)

    if question.authoring_source == AuthoringSource.AI_GENERATED:
        question.authoring_source = AuthoringSource.AI_ASSISTED

    question.version += 1
    if question.editorial_status == EditorialStatus.PUBLISHED:
        question.editorial_status = EditorialStatus.CHANGES_REQUESTED
        question.published_at = None

    _snapshot(db, question, change_summary=change_summary, changed_by_id=editor_id)
    db.flush()
    return question


def _snapshot(
    db: Session,
    question: Question,
    *,
    change_summary: str,
    changed_by_id: str | None,
) -> QuestionVersion:
    """Write an immutable record of the item as it now stands.

    Upserts on (question, version): several transitions can occur at one
    version — submit, then approve, then publish — and each should update the
    snapshot's summary rather than violate the uniqueness constraint.
    """
    existing = db.execute(
        select(QuestionVersion).where(
            QuestionVersion.question_id == question.id,
            QuestionVersion.version == question.version,
        )
    ).scalar_one_or_none()

    snapshot = {
        "stem": question.stem,
        "lead_in": question.lead_in,
        "options": question.options,
        "correct_keys": question.correct_keys,
        "explanation": question.explanation,
        "references": question.references,
        "topic": question.topic,
        "blueprint_category": question.blueprint_category,
        "difficulty_band": question.difficulty_band,
    }

    if existing is not None:
        existing.editorial_status = question.editorial_status
        existing.snapshot = snapshot
        existing.change_summary = change_summary
        existing.changed_by_id = changed_by_id
        db.flush()
        return existing

    version = QuestionVersion(
        tenant_id=question.tenant_id,
        question_id=question.id,
        version=question.version,
        editorial_status=question.editorial_status,
        snapshot=snapshot,
        change_summary=change_summary,
        changed_by_id=changed_by_id,
    )
    db.add(version)
    db.flush()
    return version


def review_queue(
    db: Session,
    *,
    tenant_id: str,
    bank_id: str | None = None,
    generation_job_id: str | None = None,
    statuses: list[str] | None = None,
    limit: int = 100,
) -> list[ReviewQueueEntry]:
    """Items awaiting a human, ordered so the riskiest surface first.

    Lowest generator confidence first. That is the useful order: a reviewer
    with an hour should spend it on the items the generator itself was least
    sure about, not on whichever happens to have the earliest id.
    """
    from app.services.ai.quality import advisory_warnings

    wanted = statuses or [
        EditorialStatus.AI_DRAFT,
        EditorialStatus.DRAFT,
        EditorialStatus.IN_REVIEW,
        EditorialStatus.CHANGES_REQUESTED,
    ]
    stmt = (
        select(Question)
        .where(
            Question.tenant_id == tenant_id,
            Question.deleted_at.is_(None),
            Question.editorial_status.in_(wanted),
        )
        .order_by(Question.ai_confidence.asc().nulls_last(), Question.created_at)
        .limit(limit)
    )
    if bank_id:
        stmt = stmt.where(Question.bank_id == bank_id)
    if generation_job_id:
        stmt = stmt.where(Question.generation_job_id == generation_job_id)

    return [
        ReviewQueueEntry(
            question_id=q.id,
            stem=q.stem,
            topic=q.topic,
            blueprint_category=q.blueprint_category,
            difficulty_band=q.difficulty_band,
            editorial_status=q.editorial_status,
            authoring_source=q.authoring_source,
            ai_confidence=q.ai_confidence,
            generation_job_id=q.generation_job_id,
            version=q.version,
            advisory_notes=advisory_warnings(
                {
                    "stem": q.stem,
                    "lead_in": q.lead_in,
                    "ai_confidence": q.ai_confidence,
                }
            ),
            created_at=q.created_at,
        )
        for q in db.execute(stmt).scalars()
    ]


def queue_summary(db: Session, *, tenant_id: str) -> dict[str, Any]:
    """Counts by status and provenance, for the dashboard."""
    rows = db.execute(
        select(
            Question.editorial_status,
            Question.authoring_source,
            func.count(Question.id),
        )
        .where(Question.tenant_id == tenant_id, Question.deleted_at.is_(None))
        .group_by(Question.editorial_status, Question.authoring_source)
    ).all()

    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}
    awaiting = 0
    unreviewed_ai = 0
    for status, source, count in rows:
        by_status[status] = by_status.get(status, 0) + count
        by_source[source] = by_source.get(source, 0) + count
        if status in (
            EditorialStatus.AI_DRAFT,
            EditorialStatus.DRAFT,
            EditorialStatus.IN_REVIEW,
            EditorialStatus.CHANGES_REQUESTED,
        ):
            awaiting += count
        if status == EditorialStatus.AI_DRAFT and source in (
            AuthoringSource.AI_GENERATED,
            AuthoringSource.AI_ASSISTED,
        ):
            unreviewed_ai += count

    return {
        "by_status": by_status,
        "by_authoring_source": by_source,
        "awaiting_review": awaiting,
        "unreviewed_ai_generated": unreviewed_ai,
        "published": by_status.get(EditorialStatus.PUBLISHED, 0),
    }


# --------------------------------------------------------------------------
# CME resources
# --------------------------------------------------------------------------
def review_resource(
    db: Session,
    resource: CmeResource,
    *,
    reviewer_id: str,
    decision: str,
    comments: str | None = None,
    now: datetime | None = None,
) -> CmeResource:
    """The same gate, applied to articles.

    Articles have no separate review table — the resource carries the reviewer,
    the timestamp and the notes — because an article's review history is far
    shorter than an item's and does not need item-level version comparison.
    """
    now = now or utcnow()
    if decision not in DECISIONS:
        raise EditorialError(
            f"'{decision}' is not a review decision. "
            f"Expected one of: {', '.join(sorted(DECISIONS))}."
        )
    if decision in COMMENT_REQUIRED and not (comments or "").strip():
        raise EditorialError(
            f"A '{decision}' decision must say why, so the author can act on it."
        )

    target = DECISIONS[decision]
    _assert_transition(resource.editorial_status, target)

    resource.editorial_status = target
    resource.reviewed_by_id = reviewer_id
    resource.reviewed_at = now
    resource.review_notes = (comments or "").strip() or None
    if target == EditorialStatus.RETIRED:
        resource.is_active = False
    db.flush()
    return resource


def ai_content_disclosure(item: Question | CmeResource) -> dict[str, Any]:
    """How the interface should label this content's provenance.

    Returned with every item and article the API serves. The label is derived
    here rather than in the frontend so that every surface — the web app, an
    exported PDF, a printed paper — says the same thing.
    """
    source = item.authoring_source
    status = item.editorial_status
    is_ai = source in (AuthoringSource.AI_GENERATED, AuthoringSource.AI_ASSISTED)
    reviewed = status in (EditorialStatus.APPROVED, EditorialStatus.PUBLISHED)

    if not is_ai:
        label = None
    elif source == AuthoringSource.AI_ASSISTED:
        label = (
            "AI-assisted, edited and reviewed by a clinician"
            if reviewed
            else "AI-assisted — awaiting clinician review"
        )
    else:
        label = (
            "AI-generated, reviewed and approved by a clinician"
            if reviewed
            else "AI-generated — NOT YET REVIEWED"
        )

    return {
        "authoring_source": source,
        "editorial_status": status,
        "is_ai_generated": is_ai,
        "is_reviewed": reviewed,
        "label": label,
        "must_display_label": is_ai,
    }
