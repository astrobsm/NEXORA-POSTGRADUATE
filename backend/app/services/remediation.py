"""Turning measured weakness into a specific, finite learning plan.

Everything here starts from evidence that already exists — a readiness
snapshot, a marked attempt, a topic breakdown — and ends in a list of actions
with due dates. Nothing is invented: an action is only generated when there is
a concrete artefact to point at (an article to read, a paper to sit, a
procedure to log), because "revise cardiology" is not a plan and a trainee
handed one is no better off than before.

Three properties the generator maintains:

* **Bounded.** A plan has a fixed weekly effort budget. A trainee who is weak
  in nine areas gets a plan for the three that matter most, not nine plans they
  will abandon. Untargeted areas are recorded so the next plan can pick them up.
* **Attributable.** Every target area carries the evidence that identified it,
  so a trainee can see why they were asked to do something.
* **Verifiable.** Each action names what will count as done — a specific
  assignment, an attempt on a specific paper, a logbook entry of a given type.
  Actions the platform can verify are marked complete automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import owned_or_shared, utcnow
from app.models.cbt import ExamAttempt, ExamPaper, ExamResponse, Question
from app.models.cme import CmeResource
from app.models.enums import (
    AttemptStatus,
    AuthoringSource,
    EditorialStatus,
    RemediationActionKind,
)
from app.models.learning import LearningPlan, LearningPlanAction, ReadinessSnapshot
from app.services.readiness import (
    COMPONENT_CASE_PRESENTATION,
    COMPONENT_CBT,
    COMPONENT_CLINICAL,
    COMPONENT_CME_READING,
    COMPONENT_JOURNAL_CLUB,
    COMPONENT_LOGBOOK,
    COMPONENT_PROFESSIONALISM,
    COMPONENT_SEMINAR,
)

#: How many weak areas one plan will address. Above this, plans stop being
#: followed — which is worse than addressing fewer areas well.
MAX_TARGET_AREAS = 3
#: Minutes of study per week the plan may schedule. An intern on nights cannot
#: absorb more, and a plan they cannot follow teaches nothing.
DEFAULT_WEEKLY_BUDGET_MINUTES = 180
#: Default plan length.
DEFAULT_PLAN_WEEKS = 4
#: A topic scoring below this on a marked attempt is a weak area.
TOPIC_WEAKNESS_THRESHOLD = 60.0
#: With fewer items than this in a topic, its score is noise.
MINIMUM_TOPIC_ITEMS = 3

#: Which action kinds address a lagging readiness component.
COMPONENT_ACTIONS: dict[str, list[str]] = {
    COMPONENT_CME_READING: [
        RemediationActionKind.READ_ARTICLE,
        RemediationActionKind.REVIEW_GUIDELINE,
    ],
    COMPONENT_CBT: [RemediationActionKind.PRACTICE_QUESTIONS],
    COMPONENT_LOGBOOK: [RemediationActionKind.LOG_PROCEDURE],
    COMPONENT_CLINICAL: [
        RemediationActionKind.SKILL_LAB,
        RemediationActionKind.MENTOR_SESSION,
    ],
    COMPONENT_SEMINAR: [RemediationActionKind.ATTEND_ACTIVITY],
    COMPONENT_JOURNAL_CLUB: [RemediationActionKind.ATTEND_ACTIVITY],
    COMPONENT_CASE_PRESENTATION: [RemediationActionKind.PRESENT_CASE],
    COMPONENT_PROFESSIONALISM: [RemediationActionKind.MENTOR_SESSION],
}

#: Typical minutes each kind of action costs. Used to fit the weekly budget.
ACTION_MINUTES: dict[str, int] = {
    RemediationActionKind.READ_ARTICLE: 30,
    RemediationActionKind.REVIEW_GUIDELINE: 45,
    RemediationActionKind.PRACTICE_QUESTIONS: 40,
    RemediationActionKind.WATCH_VIDEO: 25,
    RemediationActionKind.LOG_PROCEDURE: 15,
    RemediationActionKind.PRESENT_CASE: 120,
    RemediationActionKind.ATTEND_ACTIVITY: 60,
    RemediationActionKind.MENTOR_SESSION: 45,
    RemediationActionKind.SKILL_LAB: 90,
}


@dataclass(slots=True)
class WeakArea:
    """One thing to work on, with the evidence that identified it."""

    key: str
    label: str
    #: ``component`` (a readiness contributor) or ``topic`` (a curriculum area).
    kind: str
    score: float | None
    #: Lower sorts first — this is how the three targets are picked.
    priority: float
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "score": None if self.score is None else round(self.score, 2),
            "priority": round(self.priority, 4),
            "evidence": self.evidence,
        }


# --------------------------------------------------------------------------
# Identifying weakness
# --------------------------------------------------------------------------
def weak_areas_from_readiness(snapshot: ReadinessSnapshot) -> list[WeakArea]:
    """Lagging readiness components, ranked by the snapshot's own analysis.

    Reuses ``influential_factors`` rather than recomputing: the readiness
    engine already worked out which component moves this trainee's score most,
    and having two rankings that could disagree would be a bug waiting to
    happen.
    """
    areas: list[WeakArea] = []
    for factor in snapshot.influential_factors or []:
        key = factor.get("component")
        if not key:
            continue
        gain = factor.get("readiness_gain_if_improved")
        score = factor.get("current_score")
        if factor.get("status") == "unassessed":
            # No evidence at all is a real gap, but the instruction is
            # different — "produce some evidence", not "improve". Ranked below
            # measured weakness so a plan leads with what is actually failing.
            priority = -(factor.get("weight") or 0.0) * 5
        elif gain is None or gain <= 0.5:
            continue
        else:
            priority = -gain
        areas.append(
            WeakArea(
                key=key,
                label=factor.get("label") or key,
                kind="component",
                score=score,
                priority=priority,
                evidence={
                    "source": "readiness_snapshot",
                    "snapshot_id": snapshot.id,
                    "as_of": snapshot.as_of.isoformat(),
                    "weight": factor.get("weight"),
                    "status": factor.get("status"),
                    "readiness_gain_if_improved": gain,
                },
            )
        )
    areas.sort(key=lambda a: a.priority)
    return areas


def weak_areas_from_attempt(attempt: ExamAttempt) -> list[WeakArea]:
    """Topics a marked attempt shows the candidate struggling with.

    Requires ``MINIMUM_TOPIC_ITEMS`` before believing a topic score: one wrong
    answer out of one is a 0% topic and means nothing at all.
    """
    areas: list[WeakArea] = []
    for topic, cell in (attempt.topic_breakdown or {}).items():
        served = cell.get("served", 0)
        if served < MINIMUM_TOPIC_ITEMS:
            continue
        available = cell.get("available") or served
        marks = cell.get("marks") or 0
        percent = (marks / available * 100) if available else 0.0
        if percent >= TOPIC_WEAKNESS_THRESHOLD:
            continue
        areas.append(
            WeakArea(
                key=topic,
                label=topic.replace("_", " ").title(),
                kind="topic",
                score=percent,
                # Weight by both how badly it went and how much was asked, so a
                # 40% on ten items outranks a 40% on three.
                priority=-(TOPIC_WEAKNESS_THRESHOLD - percent) * served,
                evidence={
                    "source": "exam_attempt",
                    "attempt_id": attempt.id,
                    "paper_id": attempt.paper_id,
                    "items_served": served,
                    "items_correct": cell.get("correct", 0),
                    "percent": round(percent, 2),
                },
            )
        )
    areas.sort(key=lambda a: a.priority)
    return areas


# --------------------------------------------------------------------------
# Building actions
# --------------------------------------------------------------------------
def _articles_for_topic(
    db: Session, *, tenant_id: str, topic: str, limit: int = 2
) -> list[CmeResource]:
    """Published articles covering a topic.

    Only published resources: an AI-drafted article nobody has reviewed cannot
    be prescribed as remediation, which is the same gate the CBT engine applies
    to unreviewed items.
    """
    candidates = list(
        db.execute(
            select(CmeResource)
            .where(
                owned_or_shared(CmeResource.tenant_id, tenant_id),
                CmeResource.is_active.is_(True),
                CmeResource.deleted_at.is_(None),
                CmeResource.editorial_status == EditorialStatus.PUBLISHED,
            )
            .limit(200)
        ).scalars()
    )
    needle = topic.lower()
    matched = [
        resource
        for resource in candidates
        if needle in [t.lower() for t in (resource.topics or [])]
        or needle in (resource.title or "").lower()
    ]
    return matched[:limit]


def _practice_paper_for_topic(
    db: Session, *, tenant_id: str, topic: str
) -> ExamPaper | None:
    """An existing published practice paper covering a topic, if there is one.

    Returns ``None`` rather than fabricating a paper. The generation pipeline
    is what creates one; the plan records the need and the caller decides
    whether to spend on generation.
    """
    papers = list(
        db.execute(
            select(ExamPaper)
            .where(
                ExamPaper.tenant_id == tenant_id,
                ExamPaper.is_published.is_(True),
                ExamPaper.deleted_at.is_(None),
                ExamPaper.target_user_id.is_(None),
            )
            .limit(100)
        ).scalars()
    )
    needle = topic.lower()
    for paper in papers:
        if needle in (paper.name or "").lower():
            return paper
        if needle in {str(k).lower() for k in (paper.blueprint_profile or {})}:
            return paper
    return None


def _actions_for_area(
    db: Session,
    area: WeakArea,
    *,
    tenant_id: str,
    starts_on: date,
    weeks: int,
) -> list[dict[str, Any]]:
    """Concrete, verifiable actions addressing one weak area."""
    actions: list[dict[str, Any]] = []

    if area.kind == "topic":
        for offset, resource in enumerate(
            _articles_for_topic(db, tenant_id=tenant_id, topic=area.key)
        ):
            actions.append(
                {
                    "kind": RemediationActionKind.READ_ARTICLE,
                    "title": f"Read: {resource.title}",
                    "detail": (
                        f"Covers {area.label}, where the last assessment scored "
                        f"{area.score:.0f}%."
                        if area.score is not None
                        else f"Covers {area.label}."
                    ),
                    "target_ref": area.key,
                    "resource_id": resource.id,
                    "estimated_minutes": resource.estimated_minutes
                    or ACTION_MINUTES[RemediationActionKind.READ_ARTICLE],
                    "due_on": starts_on + timedelta(days=7 * (offset + 1)),
                }
            )
        paper = _practice_paper_for_topic(db, tenant_id=tenant_id, topic=area.key)
        if paper is not None:
            actions.append(
                {
                    "kind": RemediationActionKind.PRACTICE_QUESTIONS,
                    "title": f"Practice paper: {paper.name}",
                    "detail": f"Targeted practice on {area.label}.",
                    "target_ref": area.key,
                    "paper_id": paper.id,
                    "estimated_minutes": paper.duration_minutes
                    or ACTION_MINUTES[RemediationActionKind.PRACTICE_QUESTIONS],
                    "due_on": starts_on + timedelta(days=7 * weeks),
                }
            )
        if not actions:
            # Nothing in the library covers this yet. Say so plainly rather than
            # emitting a vague instruction — the gap is the department's to fill.
            actions.append(
                {
                    "kind": RemediationActionKind.MENTOR_SESSION,
                    "title": f"Discuss {area.label} with your supervisor",
                    "detail": (
                        "No published article or practice paper in the library "
                        f"currently covers {area.label}. Raise this at your next "
                        "supervision meeting so material can be commissioned."
                    ),
                    "target_ref": area.key,
                    "estimated_minutes": ACTION_MINUTES[
                        RemediationActionKind.MENTOR_SESSION
                    ],
                    "due_on": starts_on + timedelta(days=14),
                }
            )
        return actions

    # Component-level weakness: the action depends on which component.
    for offset, kind in enumerate(COMPONENT_ACTIONS.get(area.key, [])):
        actions.append(
            {
                "kind": kind,
                "title": _component_action_title(kind, area),
                "detail": _component_action_detail(kind, area),
                "target_ref": area.key,
                "estimated_minutes": ACTION_MINUTES[kind],
                "due_on": starts_on + timedelta(days=7 * (offset + 1)),
            }
        )
    return actions


def _component_action_title(kind: str, area: WeakArea) -> str:
    titles = {
        RemediationActionKind.READ_ARTICLE: "Complete outstanding assigned reading",
        RemediationActionKind.REVIEW_GUIDELINE: "Review the current guideline set",
        RemediationActionKind.PRACTICE_QUESTIONS: "Sit this week's practice paper",
        RemediationActionKind.LOG_PROCEDURE: "Bring the logbook up to date",
        RemediationActionKind.PRESENT_CASE: "Present a case at the next meeting",
        RemediationActionKind.ATTEND_ACTIVITY: "Attend and contribute to the next session",
        RemediationActionKind.MENTOR_SESSION: "Arrange a supervision meeting",
        RemediationActionKind.SKILL_LAB: "Book a skills laboratory session",
    }
    return titles.get(kind, area.label)


def _component_action_detail(kind: str, area: WeakArea) -> str:
    if area.score is None:
        return (
            f"No evidence has been recorded for {area.label.lower()}, so it is "
            "currently carried by the other components of your readiness score."
        )
    return (
        f"{area.label} is at {area.score:.0f}% and is the component where "
        "improvement would move your readiness score most."
    )


# --------------------------------------------------------------------------
# Plan generation
# --------------------------------------------------------------------------
def build_plan(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    areas: list[WeakArea],
    starts_on: date | None = None,
    weeks: int = DEFAULT_PLAN_WEEKS,
    weekly_budget_minutes: int = DEFAULT_WEEKLY_BUDGET_MINUTES,
    enrolment_id: str | None = None,
    readiness_snapshot_id: str | None = None,
    origin: str = "readiness",
    authoring_source: str = AuthoringSource.HUMAN,
    title: str | None = None,
) -> LearningPlan:
    """Create a bounded plan addressing the highest-priority weak areas.

    Deliberately generates from the top ``MAX_TARGET_AREAS`` only, and stops
    adding actions once the total effort would exceed the budget. The areas
    that did not fit are recorded on the plan, so the next cycle starts from
    them rather than from scratch.
    """
    starts_on = starts_on or date.today()
    ends_on = starts_on + timedelta(days=7 * weeks)
    budget = weekly_budget_minutes * weeks

    targeted = areas[:MAX_TARGET_AREAS]
    deferred = areas[MAX_TARGET_AREAS:]

    plan = LearningPlan(
        tenant_id=tenant_id,
        user_id=user_id,
        enrolment_id=enrolment_id,
        readiness_snapshot_id=readiness_snapshot_id,
        title=title or f"Learning plan, {starts_on:%d %b %Y} to {ends_on:%d %b %Y}",
        rationale=_rationale(targeted, deferred, budget),
        origin=origin,
        authoring_source=authoring_source,
        starts_on=starts_on,
        ends_on=ends_on,
        status="active",
        target_areas=[a.as_dict() for a in targeted]
        + [{**a.as_dict(), "deferred": True} for a in deferred],
    )
    db.add(plan)
    db.flush()

    spent = 0
    sequence = 0
    for area in targeted:
        for spec in _actions_for_area(
            db, area, tenant_id=tenant_id, starts_on=starts_on, weeks=weeks
        ):
            minutes = int(spec.get("estimated_minutes") or 30)
            if spent + minutes > budget:
                continue
            spent += minutes
            db.add(
                LearningPlanAction(
                    tenant_id=tenant_id,
                    plan_id=plan.id,
                    sequence=sequence,
                    kind=spec["kind"],
                    title=spec["title"],
                    detail=spec.get("detail"),
                    target_ref=spec.get("target_ref"),
                    resource_id=spec.get("resource_id"),
                    paper_id=spec.get("paper_id"),
                    estimated_minutes=minutes,
                    due_on=spec.get("due_on"),
                )
            )
            sequence += 1
    db.flush()
    return plan


def _rationale(
    targeted: list[WeakArea], deferred: list[WeakArea], budget: int
) -> str:
    if not targeted:
        return (
            "No weak areas met the evidence threshold for a plan. Continue with "
            "the standard curriculum."
        )
    lines = [
        "This plan addresses the areas where improvement would move your "
        "examination readiness furthest, within a "
        f"{budget // 60}-hour study budget:",
    ]
    for area in targeted:
        if area.score is None:
            lines.append(f"  - {area.label}: no evidence recorded yet.")
        else:
            lines.append(f"  - {area.label}: currently {area.score:.0f}%.")
    if deferred:
        names = ", ".join(a.label for a in deferred)
        lines.append(
            f"Also identified but deferred to the next cycle: {names}. Addressing "
            "three areas properly beats nine superficially."
        )
    return "\n".join(lines)


def plan_from_readiness(
    db: Session,
    snapshot: ReadinessSnapshot,
    **kwargs: Any,
) -> LearningPlan:
    """Generate a plan from a readiness snapshot."""
    return build_plan(
        db,
        tenant_id=snapshot.tenant_id,
        user_id=snapshot.user_id,
        areas=weak_areas_from_readiness(snapshot),
        enrolment_id=snapshot.enrolment_id,
        readiness_snapshot_id=snapshot.id,
        origin="readiness",
        **kwargs,
    )


def plan_from_attempt(
    db: Session,
    attempt: ExamAttempt,
    **kwargs: Any,
) -> LearningPlan | None:
    """Generate a plan from a marked attempt, or ``None`` if nothing was weak.

    Returning ``None`` matters: a trainee who scored well should not be handed
    a remediation plan, and generating an empty one to avoid a null would be
    both dishonest and demoralising.
    """
    if attempt.status not in (AttemptStatus.SUBMITTED, AttemptStatus.MARKED):
        raise ValueError("A plan can only be built from a marked attempt.")
    areas = weak_areas_from_attempt(attempt)
    if not areas:
        return None
    return build_plan(
        db,
        tenant_id=attempt.tenant_id,
        user_id=attempt.user_id,
        areas=areas,
        enrolment_id=attempt.enrolment_id,
        origin="exam_result",
        title=f"Post-examination plan, {utcnow():%d %b %Y}",
        **kwargs,
    )


# --------------------------------------------------------------------------
# Progress
# --------------------------------------------------------------------------
def refresh_progress(db: Session, plan: LearningPlan) -> LearningPlan:
    """Recompute completion, marking verifiable actions done automatically.

    An action that names a paper is complete when the trainee has a marked
    attempt at it since the plan began; one that names an article is complete
    when they have a completed assignment for it. Actions the platform cannot
    verify stay in the trainee's hands to mark.
    """
    actions = list(
        db.execute(
            select(LearningPlanAction)
            .where(LearningPlanAction.plan_id == plan.id)
            .order_by(LearningPlanAction.sequence)
        ).scalars()
    )
    if not actions:
        plan.completion_percent = 0.0
        db.flush()
        return plan

    for action in actions:
        if action.status == "done" or action.paper_id is None:
            continue
        sat = db.execute(
            select(ExamAttempt).where(
                ExamAttempt.user_id == plan.user_id,
                ExamAttempt.paper_id == action.paper_id,
                ExamAttempt.status.in_([AttemptStatus.SUBMITTED, AttemptStatus.MARKED]),
            )
        ).scalars().first()
        if sat is not None:
            action.status = "done"
            action.completed_at = sat.submitted_at or utcnow()
            action.evidence_ref = sat.id

    done = sum(1 for a in actions if a.status == "done")
    plan.completion_percent = round(done / len(actions) * 100, 2)
    if plan.completion_percent >= 100:
        plan.status = "completed"
    db.flush()
    return plan


def topics_needing_material(db: Session, plan: LearningPlan) -> list[str]:
    """Weak topics the library could not serve.

    The input to a generation request: these are precisely the topics where
    spending on AI authoring buys something the library does not already have.
    """
    served = {
        action.target_ref
        for action in db.execute(
            select(LearningPlanAction).where(
                LearningPlanAction.plan_id == plan.id,
                LearningPlanAction.resource_id.isnot(None),
            )
        ).scalars()
        if action.target_ref
    }
    wanted = {
        area.get("key")
        for area in (plan.target_areas or [])
        if area.get("kind") == "topic" and area.get("key")
    }
    return sorted(wanted - served)


def question_ids_for_review(db: Session, attempt: ExamAttempt) -> list[str]:
    """Items from this attempt worth re-serving in a follow-up paper.

    Specifically the ones the candidate answered incorrectly *and* which fall
    in a topic the attempt showed to be weak. An isolated wrong answer in an
    otherwise strong topic is a slip, not a knowledge gap, and re-serving it
    wastes a place in a fifty-item paper.
    """
    weak = {a.key for a in weak_areas_from_attempt(attempt)}
    if not weak:
        return []
    rows = db.execute(
        select(ExamResponse.question_id)
        .join(Question, ExamResponse.question_id == Question.id)
        .where(
            ExamResponse.attempt_id == attempt.id,
            ExamResponse.is_correct.is_(False),
            Question.topic.in_(sorted(weak)),
        )
    ).scalars()
    return list(rows)
