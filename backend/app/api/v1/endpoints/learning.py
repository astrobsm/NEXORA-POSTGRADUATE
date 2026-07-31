"""Reading engagement, examination readiness and remediation.

Three scoping rules run through every endpoint here, because all three
subsystems handle data about a named trainee:

* a trainee may always read their own record;
* a supervisor needs ``analytics.supervised.read`` for someone else's;
* reading *annotations* are private to their author with no override. A
  trainee's marginal notes are their study material, not evidence, and a
  supervisor has no legitimate need for them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, TenantId
from app.models.cme import CmeResource
from app.models.enums import AuditAction
from app.models.learning import (
    EngagementSnapshot,
    LearningPlan,
    LearningPlanAction,
    ReadinessSnapshot,
    ReadingAnnotation,
    ReadingSession,
)
from app.services import audit, editorial, readiness, reading, remediation

router = APIRouter()

#: The default analysis window. Ninety days is a training quarter — long enough
#: for a consistency score to mean something, short enough that a trainee who
#: turned things around three months ago is not still carrying it.
DEFAULT_WINDOW_DAYS = 90


class OpenSessionIn(BaseModel):
    resource_id: str
    assignment_id: str | None = None
    captured_offline: bool = False


class ReadingEventIn(BaseModel):
    kind: str
    occurred_at: datetime | None = None
    delta_seconds: int = 0
    scroll_percent: float | None = None
    section_ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AnnotationIn(BaseModel):
    kind: str = Field(pattern="^(highlight|note|bookmark)$")
    body: str | None = None
    quoted_text: str | None = None
    section_ref: str | None = None
    anchor: dict[str, Any] = Field(default_factory=dict)
    colour: str | None = None


class PlanActionUpdateIn(BaseModel):
    status: str = Field(pattern="^(pending|in_progress|done|skipped)$")


def _authorise(principal: CurrentPrincipal, user_id: str | None) -> str:
    """Resolve whose record is being asked for, and check the caller may see it."""
    target = user_id or principal.id
    if target != principal.id:
        principal.require("analytics.supervised.read")
    return target


def _window(as_of: date | None, days: int) -> tuple[date, date]:
    end = as_of or date.today()
    return end - timedelta(days=days), end


# ==========================================================================
# Reading
# ==========================================================================
@router.post("/reading/sessions", status_code=status.HTTP_201_CREATED)
def open_reading_session(
    body: OpenSessionIn,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
) -> dict[str, Any]:
    """Begin a reading session against a published resource."""
    resource = db.get(CmeResource, body.resource_id)
    if resource is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found.")
    try:
        session = reading.open_session(
            db,
            tenant_id=tenant_id,
            user_id=principal.id,
            resource=resource,
            assignment_id=body.assignment_id,
            captured_offline=body.captured_offline,
        )
    except reading.ReadingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return {
        "session_id": session.id,
        "resource_id": resource.id,
        "is_revisit": session.is_revisit,
        "section_count": len(resource.sections or []),
        "estimated_minutes": resource.estimated_minutes,
        "provenance": editorial.ai_content_disclosure(resource),
    }


@router.post("/reading/sessions/{session_id}/events", status_code=status.HTTP_202_ACCEPTED)
def post_reading_events(
    session_id: str,
    events: list[ReadingEventIn],
    db: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    """Append telemetry. Accepts batches so an offline client can drain a queue."""
    session = db.get(ReadingSession, session_id)
    if session is None or session.user_id != principal.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reading session not found.")

    for event in events:
        reading.record_event(
            db,
            session,
            kind=event.kind,
            occurred_at=event.occurred_at,
            delta_seconds=event.delta_seconds,
            scroll_percent=event.scroll_percent,
            section_ref=event.section_ref,
            payload=event.payload,
        )

    resource = session.resource or db.get(CmeResource, session.resource_id)
    return {
        "session_id": session.id,
        "active_seconds": session.active_seconds,
        "max_scroll_percent": session.max_scroll_percent,
        "section_completion_percent": session.section_completion_percent,
        "completion_percent": (
            reading.session_completion(session, resource) if resource else 0.0
        ),
    }


@router.post("/reading/sessions/{session_id}/recompute")
def recompute_reading_session(
    session_id: str, db: DbSession, principal: CurrentPrincipal
) -> dict[str, Any]:
    """Rebuild a session's totals from its raw event stream.

    The reconciliation path after an offline sync, where events can arrive out
    of order or twice.
    """
    session = db.get(ReadingSession, session_id)
    if session is None or session.user_id != principal.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reading session not found.")
    reading.recompute_session(db, session)
    return {
        "session_id": session.id,
        "active_seconds": session.active_seconds,
        "sections_completed": session.sections_completed,
    }


@router.post("/reading/sessions/{session_id}/annotations", status_code=status.HTTP_201_CREATED)
def add_annotation(
    session_id: str,
    body: AnnotationIn,
    db: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    session = db.get(ReadingSession, session_id)
    if session is None or session.user_id != principal.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reading session not found.")
    annotation = reading.add_annotation(
        db,
        session,
        kind=body.kind,
        body=body.body,
        quoted_text=body.quoted_text,
        section_ref=body.section_ref,
        anchor=body.anchor,
        colour=body.colour,
    )
    return {"annotation_id": annotation.id, "kind": annotation.kind}


@router.get("/reading/annotations")
def list_annotations(
    db: DbSession,
    principal: CurrentPrincipal,
    resource_id: str | None = None,
) -> list[dict[str, Any]]:
    """The caller's own highlights, notes and bookmarks.

    There is deliberately no ``user_id`` parameter and no supervisory override.
    Annotations are private study material; a supervisor who needs to know how
    much a trainee read has the engagement scores for that.
    """
    stmt = select(ReadingAnnotation).where(
        ReadingAnnotation.user_id == principal.id,
        ReadingAnnotation.deleted_at.is_(None),
    )
    if resource_id:
        stmt = stmt.where(ReadingAnnotation.resource_id == resource_id)
    return [
        {
            "annotation_id": a.id,
            "resource_id": a.resource_id,
            "kind": a.kind,
            "section_ref": a.section_ref,
            "anchor": a.anchor,
            "quoted_text": a.quoted_text,
            "body": a.body,
            "colour": a.colour,
            "created_at": a.created_at.isoformat(),
        }
        for a in db.execute(stmt.order_by(ReadingAnnotation.created_at.desc())).scalars()
    ]


@router.get("/reading/scores")
def reading_scores(
    db: DbSession,
    principal: CurrentPrincipal,
    user_id: str | None = None,
    as_of: date | None = None,
    window_days: int = Query(DEFAULT_WINDOW_DAYS, ge=7, le=365),
) -> dict[str, Any]:
    """The four reading-derived scores, with the inputs behind each."""
    target = _authorise(principal, user_id)
    start, end = _window(as_of, window_days)
    scores = reading.compute_scores(
        db, user_id=target, window_start=start, window_end=end
    )
    return {
        "user_id": target,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "reading_score": scores.reading,
        "consistency_score": scores.consistency,
        "engagement_score": scores.engagement,
        # `null` rather than 0 when there is not yet enough evidence — a
        # trainee in their first fortnight has no retention to measure.
        "retention_score": scores.retention,
        "articles_opened": scores.articles_opened,
        "articles_completed": scores.articles_completed,
        "active_minutes": scores.active_minutes,
        "distinct_active_days": scores.distinct_active_days,
        "components": scores.components,
    }


@router.post("/reading/scores/snapshot", status_code=status.HTTP_201_CREATED)
def snapshot_reading_scores(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    user_id: str | None = None,
    as_of: date | None = None,
    window_days: int = Query(DEFAULT_WINDOW_DAYS, ge=7, le=365),
) -> dict[str, Any]:
    target = _authorise(principal, user_id)
    start, end = _window(as_of, window_days)
    snapshot = reading.snapshot_scores(
        db, tenant_id=tenant_id, user_id=target, window_start=start, window_end=end
    )
    return snapshot.as_dict()


@router.get("/reading/history")
def reading_history(
    db: DbSession,
    principal: CurrentPrincipal,
    user_id: str | None = None,
    limit: int = Query(12, ge=1, le=52),
) -> list[dict[str, Any]]:
    """Stored engagement snapshots, newest first. The trend is the signal."""
    target = _authorise(principal, user_id)
    rows = db.execute(
        select(EngagementSnapshot)
        .where(EngagementSnapshot.user_id == target)
        .order_by(EngagementSnapshot.window_end.desc())
        .limit(limit)
    ).scalars()
    return [row.as_dict() for row in rows]


# ==========================================================================
# Readiness
# ==========================================================================
@router.get("/readiness")
def get_readiness(
    db: DbSession,
    principal: CurrentPrincipal,
    user_id: str | None = None,
    as_of: date | None = None,
    window_days: int = Query(DEFAULT_WINDOW_DAYS, ge=7, le=365),
) -> dict[str, Any]:
    """The Examination Readiness Score, computed live and not stored."""
    target = _authorise(principal, user_id)
    result = readiness.compute_readiness(
        db, user_id=target, as_of=as_of, window_days=window_days
    )
    return {
        "user_id": target,
        "as_of": (as_of or date.today()).isoformat(),
        "score": result.score,
        "category": result.category,
        "confidence_low": result.confidence_low,
        "confidence_high": result.confidence_high,
        # How much of the weight table had real evidence behind it. The honest
        # headline when someone asks how much of this score is measured.
        "evidence_coverage": result.evidence_coverage,
        "components": result.as_component_dict(),
        "unassessed_components": result.unassessed_keys,
        "influential_factors": result.influential_factors,
        "indices": result.indices,
        "weights_used": result.weights_used,
        "category_boundaries": {
            name: floor for floor, name in readiness.CATEGORY_FLOORS
        },
        "notes": [
            "Unassessed components are excluded and the remaining weights "
            "renormalised. They are never scored zero, because no evidence and "
            "poor performance are different things.",
            "The confidence interval widens as evidence thins. A wide band means "
            "the platform does not yet know enough to be precise.",
        ],
    }


@router.post("/readiness/snapshot", status_code=status.HTTP_201_CREATED)
def snapshot_readiness(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    user_id: str | None = None,
    as_of: date | None = None,
    enrolment_id: str | None = None,
    target_examination: str | None = None,
    target_date: date | None = None,
    window_days: int = Query(DEFAULT_WINDOW_DAYS, ge=7, le=365),
) -> dict[str, Any]:
    """Compute and store a readiness snapshot, with its delta from the last."""
    target = _authorise(principal, user_id)
    snapshot = readiness.snapshot_readiness(
        db,
        tenant_id=tenant_id,
        user_id=target,
        as_of=as_of,
        enrolment_id=enrolment_id,
        target_examination=target_examination,
        target_date=target_date,
        window_days=window_days,
    )
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id,
                 actor_id=principal.id, entity_type="readiness_snapshot",
                 entity_id=snapshot.id,
                 summary=f"Readiness {snapshot.score} ({snapshot.category})", **meta)
    return snapshot.as_dict()


@router.get("/readiness/history")
def readiness_history(
    db: DbSession,
    principal: CurrentPrincipal,
    user_id: str | None = None,
    limit: int = Query(12, ge=1, le=52),
) -> list[dict[str, Any]]:
    target = _authorise(principal, user_id)
    rows = db.execute(
        select(ReadinessSnapshot)
        .where(ReadinessSnapshot.user_id == target)
        .order_by(ReadinessSnapshot.as_of.desc())
        .limit(limit)
    ).scalars()
    return [row.as_dict() for row in rows]


# ==========================================================================
# Remediation
# ==========================================================================
@router.post("/plans/from-readiness", status_code=status.HTTP_201_CREATED)
def plan_from_readiness(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    snapshot_id: str | None = None,
    user_id: str | None = None,
    weeks: int = Query(remediation.DEFAULT_PLAN_WEEKS, ge=1, le=26),
    weekly_budget_minutes: int = Query(
        remediation.DEFAULT_WEEKLY_BUDGET_MINUTES, ge=30, le=1200
    ),
) -> dict[str, Any]:
    """Generate a bounded learning plan from a readiness snapshot."""
    target = _authorise(principal, user_id)
    if snapshot_id:
        snapshot = db.get(ReadinessSnapshot, snapshot_id)
        if snapshot is None or snapshot.user_id != target:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Snapshot not found.")
    else:
        snapshot = readiness.snapshot_readiness(
            db, tenant_id=tenant_id, user_id=target
        )

    plan = remediation.plan_from_readiness(
        db, snapshot, weeks=weeks, weekly_budget_minutes=weekly_budget_minutes
    )
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id,
                 actor_id=principal.id, entity_type="learning_plan",
                 entity_id=plan.id, summary=plan.title, **meta)
    return _plan_payload(db, plan)


@router.get("/plans")
def list_plans(
    db: DbSession,
    principal: CurrentPrincipal,
    user_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
) -> list[dict[str, Any]]:
    target = _authorise(principal, user_id)
    stmt = select(LearningPlan).where(
        LearningPlan.user_id == target, LearningPlan.deleted_at.is_(None)
    )
    if status_filter:
        stmt = stmt.where(LearningPlan.status == status_filter)
    plans = db.execute(stmt.order_by(LearningPlan.starts_on.desc())).scalars()
    return [_plan_payload(db, plan) for plan in plans]


@router.get("/plans/{plan_id}")
def get_plan(
    plan_id: str, db: DbSession, principal: CurrentPrincipal
) -> dict[str, Any]:
    plan = db.get(LearningPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found.")
    _authorise(principal, plan.user_id)
    remediation.refresh_progress(db, plan)
    return _plan_payload(db, plan)


@router.patch("/plans/{plan_id}/actions/{action_id}")
def update_plan_action(
    plan_id: str,
    action_id: str,
    body: PlanActionUpdateIn,
    db: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    """Mark an action done. Only the trainee may update their own plan."""
    from app.db.base import utcnow

    plan = db.get(LearningPlan, plan_id)
    if plan is None or plan.user_id != principal.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found.")
    action = db.get(LearningPlanAction, action_id)
    if action is None or action.plan_id != plan.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found.")

    action.status = body.status
    action.completed_at = utcnow() if body.status == "done" else None
    remediation.refresh_progress(db, plan)
    return _plan_payload(db, plan)


@router.get("/plans/{plan_id}/material-gaps")
def plan_material_gaps(
    plan_id: str, db: DbSession, principal: CurrentPrincipal
) -> dict[str, Any]:
    """Weak topics the library could not serve.

    Exactly the topics where commissioning or generating material buys
    something the institution does not already have — which is the input a
    department needs before authorising generation spend.
    """
    plan = db.get(LearningPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found.")
    _authorise(principal, plan.user_id)
    topics = remediation.topics_needing_material(db, plan)
    return {
        "plan_id": plan.id,
        "topics_without_material": topics,
        "suggestion": (
            "No published article in the library covers these. Commission or "
            "generate material before the next planning cycle."
            if topics
            else "Every targeted topic has published material available."
        ),
    }


def _plan_payload(db: DbSession, plan: LearningPlan) -> dict[str, Any]:
    actions = db.execute(
        select(LearningPlanAction)
        .where(LearningPlanAction.plan_id == plan.id)
        .order_by(LearningPlanAction.sequence)
    ).scalars()
    return {
        "plan_id": plan.id,
        "user_id": plan.user_id,
        "title": plan.title,
        "rationale": plan.rationale,
        "origin": plan.origin,
        "authoring_source": plan.authoring_source,
        "starts_on": plan.starts_on.isoformat(),
        "ends_on": plan.ends_on.isoformat(),
        "status": plan.status,
        "completion_percent": plan.completion_percent,
        "target_areas": plan.target_areas,
        "actions": [
            {
                "action_id": a.id,
                "sequence": a.sequence,
                "kind": a.kind,
                "title": a.title,
                "detail": a.detail,
                "target_ref": a.target_ref,
                "resource_id": a.resource_id,
                "paper_id": a.paper_id,
                "estimated_minutes": a.estimated_minutes,
                "due_on": a.due_on.isoformat() if a.due_on else None,
                "status": a.status,
                "completed_at": a.completed_at.isoformat() if a.completed_at else None,
            }
            for a in actions
        ],
    }
