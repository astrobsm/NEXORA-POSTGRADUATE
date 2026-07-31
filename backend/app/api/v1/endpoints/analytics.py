"""Performance analytics, the promotion engine and role dashboards."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, TenantId
from app.models.academic import AcademicActivity
from app.models.analytics import PromotionReview, ScoreSnapshot
from app.models.assessment import Assessment
from app.models.enums import (
    AuditAction,
    EnrolmentStatus,
    RagStatus,
    ValidationStatus,
)
from app.models.identity import User
from app.models.logbook import LogEntry
from app.models.research import Publication, ResearchProject
from app.models.tenancy import OrgUnit
from app.models.training import Enrolment, RotationAssignment
from app.schemas.common import PromotionAssessmentOut, ScoreReportOut
from app.services import audit, scoring
from app.services import promotion as promotion_engine

router = APIRouter()


def _load_enrolment(db, tenant_id: str, enrolment_id: str) -> Enrolment:
    enrolment = db.get(Enrolment, enrolment_id)
    if enrolment is None or enrolment.tenant_id != tenant_id or enrolment.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Enrolment not found.")
    return enrolment


def _authorise(principal, enrolment: Enrolment, *, own_permission="analytics.self.read") -> None:
    if principal.is_superuser or enrolment.trainee_id == principal.id:
        return
    if enrolment.primary_supervisor_id == principal.id and principal.has("analytics.supervised.read"):
        return
    if principal.has("analytics.department.read", org_unit_id=enrolment.org_unit_id):
        return
    if principal.has("analytics.institution.read"):
        return
    raise HTTPException(status_code=403, detail="You cannot view this trainee's analytics.")


def _report_out(report: scoring.ScoreReport, *, include_rules: bool) -> ScoreReportOut:
    return ScoreReportOut(
        enrolment_id=report.enrolment_id,
        computed_at=report.computed_at,
        training_year=report.training_year,
        overall_score=round(report.overall_score, 1),
        overall_rag=report.overall_rag,
        promotion_readiness_score=round(report.promotion_readiness_score, 1),
        domains={
            k: {
                "domain": v.domain,
                "score": round(v.score, 1),
                "rag": v.rag,
                "contributing_rules": v.contributing_rules,
                "signals": v.signals,
            }
            for k, v in report.domains.items()
        },
        gaps=report.gaps,
        metrics=report.metrics,
        weights_used={k: round(v, 4) for k, v in report.weights_used.items()},
        unassessed_domains=report.unassessed_domains,
        effective_weight_base=report.effective_weight_base,
        requirement_results=[r.to_dict() for r in report.requirement_results] if include_rules else [],
    )


# --------------------------------------------------------------------------
# Scores
# --------------------------------------------------------------------------
@router.get("/enrolments/{enrolment_id}/score", response_model=ScoreReportOut,
            summary="Compute the performance scorecard")
def get_score(
    enrolment_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    as_of: date | None = None,
    include_rules: bool = True,
    persist: bool = False,
):
    """Live computation across all eight domains, showing every requirement behind the
    numbers. Set ``persist=true`` to also write an immutable snapshot."""
    enrolment = _load_enrolment(db, tenant_id, enrolment_id)
    _authorise(principal, enrolment)

    if persist:
        principal.require("analytics.supervised.read")
        report, _ = scoring.score_and_persist(db, enrolment, as_of=as_of, trigger="on_demand")
        db.commit()
    else:
        report = scoring.compute_scores(db, enrolment, as_of=as_of)

    return _report_out(report, include_rules=include_rules)


@router.get("/enrolments/{enrolment_id}/score/history", summary="Score trend over time")
def score_history(
    enrolment_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    limit: int = Query(24, ge=1, le=200),
):
    enrolment = _load_enrolment(db, tenant_id, enrolment_id)
    _authorise(principal, enrolment)
    rows = db.execute(
        select(ScoreSnapshot)
        .where(ScoreSnapshot.enrolment_id == enrolment_id)
        .order_by(ScoreSnapshot.computed_at.desc())
        .limit(limit)
    ).scalars().all()
    return [
        {
            "computed_at": s.computed_at,
            "training_year": s.training_year,
            "overall_score": round(s.overall_score, 1),
            "overall_rag": s.overall_rag,
            "promotion_readiness_score": round(s.promotion_readiness_score, 1),
            "domains": {
                "clinical_competency": round(s.clinical_competency_score, 1),
                "research": round(s.research_score, 1),
                "academic": round(s.academic_score, 1),
                "professionalism": round(s.professionalism_score, 1),
                "leadership": round(s.leadership_score, 1),
                "attendance": round(s.attendance_score, 1),
                "teaching": round(s.teaching_score, 1),
                "exam_readiness": round(s.exam_readiness_score, 1),
            },
            "gap_count": len(s.gaps or []),
        }
        for s in reversed(rows)
    ]


@router.post("/score/recompute", summary="Recompute scores for a cohort")
def recompute_cohort(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    org_unit_id: str | None = None,
    programme_id: str | None = None,
    limit: int = Query(500, ge=1, le=2000),
):
    """Batch scoring, normally run nightly. Returns a per-trainee summary rather than
    a bare count so a coordinator can see what changed."""
    principal.require("analytics.department.read")
    stmt = select(Enrolment).where(
        Enrolment.tenant_id == tenant_id,
        Enrolment.deleted_at.is_(None),
        Enrolment.status.in_([EnrolmentStatus.ACTIVE, EnrolmentStatus.ON_LEAVE]),
    )
    if org_unit_id:
        stmt = stmt.where(Enrolment.org_unit_id == org_unit_id)
    if programme_id:
        stmt = stmt.where(Enrolment.programme_id == programme_id)

    results = []
    for enrolment in db.execute(stmt.limit(limit)).scalars():
        report, _ = scoring.score_and_persist(db, enrolment, trigger="scheduled")
        results.append(
            {
                "enrolment_id": enrolment.id,
                "trainee": enrolment.trainee.full_name if enrolment.trainee else None,
                "overall_score": round(report.overall_score, 1),
                "rag": report.overall_rag,
                "gaps": len(report.gaps),
            }
        )
    audit.record(db, action=AuditAction.UPDATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="score_snapshot", summary=f"Recomputed {len(results)} scorecards", **meta)
    db.commit()
    return {"scored": len(results), "results": results}


# --------------------------------------------------------------------------
# Promotion engine
# --------------------------------------------------------------------------
@router.get("/enrolments/{enrolment_id}/promotion", response_model=PromotionAssessmentOut,
            summary="Assess promotion readiness")
def promotion_readiness(
    enrolment_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    as_of: date | None = None,
):
    enrolment = _load_enrolment(db, tenant_id, enrolment_id)
    _authorise(principal, enrolment)
    assessment = promotion_engine.assess(db, enrolment, as_of=as_of, include_scores=False)
    return PromotionAssessmentOut(
        enrolment_id=assessment.enrolment_id,
        from_level=assessment.from_level,
        to_level=assessment.to_level,
        from_year=assessment.from_year,
        to_year=assessment.to_year,
        outcome=assessment.outcome,
        readiness_percent=round(assessment.readiness_percent, 1),
        rationale=assessment.rationale,
        time_served_months=assessment.time_served_months,
        minimum_months_required=assessment.minimum_months_required,
        blocking=assessment.blocking,
        advisories=assessment.advisories,
        checks=assessment.checks,
    )


@router.get("/enrolments/{enrolment_id}/exam-eligibility", summary="College exam eligibility")
def exam_eligibility(enrolment_id: str, db: DbSession, principal: CurrentPrincipal,
                     tenant_id: TenantId, as_of: date | None = None):
    enrolment = _load_enrolment(db, tenant_id, enrolment_id)
    _authorise(principal, enrolment)
    return promotion_engine.exam_eligibility(db, enrolment, as_of=as_of)


@router.post("/enrolments/{enrolment_id}/promotion/review", summary="Open a promotion review")
def open_review(
    enrolment_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    principal.require("promotion.decide")
    enrolment = _load_enrolment(db, tenant_id, enrolment_id)
    assessment = promotion_engine.assess(db, enrolment)
    review = promotion_engine.record_review(db, enrolment, assessment)
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="promotion_review", entity_id=review.id,
                 summary=f"Promotion review opened: engine says {assessment.outcome}", **meta)
    db.commit()
    db.refresh(review)
    return {
        "review_id": review.id,
        "engine_outcome": review.engine_outcome,
        "readiness_percent": round(review.engine_readiness_percent, 1),
        "rationale": review.engine_rationale,
        "blocking_requirements": review.blocking_requirements,
    }


@router.post("/promotion/reviews/{review_id}/decision", summary="Record the committee decision")
def decide_promotion(
    review_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    outcome: str = Query(description="approved | declined | deferred | conditional"),
    effective_date: date | None = None,
    note: str | None = None,
    override_reason: str | None = None,
    conditions: list[str] | None = Query(default=None),
):
    """Ratify or overturn the engine's recommendation. A decision that contradicts the
    engine is accepted, but only with a recorded reason."""
    principal.require("promotion.decide")
    review = db.get(PromotionReview, review_id)
    if review is None or review.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Promotion review not found.")
    try:
        promotion_engine.apply_decision(
            db, review, outcome=outcome, decided_by_id=principal.id,
            effective_date=effective_date, conditions=conditions, note=note,
            override_reason=override_reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    audit.record(db, action=AuditAction.APPROVE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="promotion_review", entity_id=review.id,
                 summary=f"Promotion decision: {outcome}"
                         + (f" (override: {override_reason})" if override_reason else ""), **meta)
    db.commit()
    db.refresh(review)
    return {
        "review_id": review.id,
        "outcome": review.outcome,
        "effective_date": review.effective_date,
        "engine_outcome": review.engine_outcome,
        "was_override": review.override_reason is not None,
    }


@router.get("/promotion/cohort", summary="Cohort promotion readiness")
def cohort_readiness(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    org_unit_id: str | None = None,
    programme_id: str | None = None,
    training_year: int | None = None,
):
    principal.require("promotion.readiness.read")
    stmt = select(Enrolment).where(
        Enrolment.tenant_id == tenant_id,
        Enrolment.deleted_at.is_(None),
        Enrolment.status == EnrolmentStatus.ACTIVE,
    )
    if org_unit_id:
        stmt = stmt.where(Enrolment.org_unit_id == org_unit_id)
    if programme_id:
        stmt = stmt.where(Enrolment.programme_id == programme_id)
    if training_year:
        stmt = stmt.where(Enrolment.current_year == training_year)

    enrolments = list(db.execute(stmt).scalars().all())
    assessments = promotion_engine.batch_assess(db, enrolments)
    by_outcome = Counter(a.outcome for a in assessments)

    return {
        "total": len(assessments),
        "summary": dict(by_outcome),
        "trainees": [
            {
                "enrolment_id": a.enrolment_id,
                "trainee": next(
                    (e.trainee.full_name for e in enrolments
                     if e.id == a.enrolment_id and e.trainee), None
                ),
                "from_level": a.from_level,
                "to_level": a.to_level,
                "outcome": a.outcome,
                "readiness_percent": round(a.readiness_percent, 1),
                "blocking_count": len(a.blocking),
                "months_served": a.time_served_months,
                "months_required": a.minimum_months_required,
                "rationale": a.rationale,
            }
            for a in sorted(assessments, key=lambda x: -x.readiness_percent)
        ],
    }


# --------------------------------------------------------------------------
# Dashboards
# --------------------------------------------------------------------------
@router.get("/dashboard/trainee", summary="Trainee dashboard")
def trainee_dashboard(db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    enrolment = db.execute(
        select(Enrolment)
        .where(Enrolment.trainee_id == principal.id, Enrolment.deleted_at.is_(None))
        .order_by(Enrolment.start_date.desc())
    ).scalars().first()
    if enrolment is None:
        return {"has_enrolment": False}

    today = date.today()
    report = scoring.compute_scores(db, enrolment)
    current = enrolment.current_rotation(today)
    pending_logs = db.execute(
        select(func.count()).select_from(LogEntry).where(
            LogEntry.enrolment_id == enrolment.id,
            LogEntry.validation_status == ValidationStatus.PENDING,
            LogEntry.deleted_at.is_(None),
        )
    ).scalar_one()
    upcoming = db.execute(
        select(AcademicActivity)
        .where(
            AcademicActivity.org_unit_id == enrolment.org_unit_id,
            AcademicActivity.scheduled_on >= today,
            AcademicActivity.scheduled_on <= today + timedelta(days=14),
            AcademicActivity.deleted_at.is_(None),
        )
        .order_by(AcademicActivity.scheduled_at.asc())
        .limit(5)
    ).scalars().all()
    promotion = promotion_engine.assess(db, enrolment, include_scores=False)

    return {
        "has_enrolment": True,
        "enrolment": {
            "id": enrolment.id,
            "programme": enrolment.programme.name if enrolment.programme else None,
            "level": enrolment.current_level,
            "year": enrolment.current_year,
            "start_date": str(enrolment.start_date),
            "expected_end_date": str(enrolment.expected_end_date),
            "months_served": promotion.time_served_months,
        },
        "scores": _report_out(report, include_rules=False),
        "current_rotation": (
            {
                "id": current.id,
                "name": current.name,
                "start_date": str(current.start_date),
                "end_date": str(current.end_date),
                "days_remaining": (current.end_date - today).days,
                "supervisor": current.supervisor.full_name if current.supervisor else None,
                "completion_percent": current.completion_percent,
            }
            if current
            else None
        ),
        "pending_validations": int(pending_logs),
        "promotion": {
            "outcome": promotion.outcome,
            "readiness_percent": round(promotion.readiness_percent, 1),
            "blocking_count": len(promotion.blocking),
        },
        "top_gaps": report.gaps[:5],
        "upcoming_activities": [
            {"id": a.id, "title": a.title, "kind": a.kind, "scheduled_at": a.scheduled_at,
             "venue": a.venue}
            for a in upcoming
        ],
    }


@router.get("/dashboard/supervisor", summary="Consultant dashboard")
def supervisor_dashboard(db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    today = date.today()
    supervised = db.execute(
        select(Enrolment).where(
            Enrolment.tenant_id == tenant_id,
            Enrolment.deleted_at.is_(None),
            Enrolment.primary_supervisor_id == principal.id,
        )
    ).scalars().all()
    rotation_supervised = db.execute(
        select(RotationAssignment).where(
            RotationAssignment.tenant_id == tenant_id,
            RotationAssignment.supervisor_id == principal.id,
            RotationAssignment.start_date <= today,
            RotationAssignment.end_date >= today,
        )
    ).scalars().all()

    pending_logs = db.execute(
        select(LogEntry).where(
            LogEntry.tenant_id == tenant_id,
            LogEntry.supervisor_id == principal.id,
            LogEntry.validation_status == ValidationStatus.PENDING,
            LogEntry.deleted_at.is_(None),
        ).order_by(LogEntry.occurred_at.asc())
    ).scalars().all()

    draft_assessments = db.execute(
        select(func.count()).select_from(Assessment).where(
            Assessment.assessor_id == principal.id,
            Assessment.status == "draft",
            Assessment.deleted_at.is_(None),
        )
    ).scalar_one()

    oldest_days = (today - pending_logs[0].occurred_on).days if pending_logs else 0
    at_risk = [e for e in supervised if e.latest_rag in {RagStatus.RED, RagStatus.AMBER}]

    return {
        "supervised_count": len(supervised),
        "active_rotations": [
            {
                "rotation_id": r.id,
                "trainee": r.enrolment.trainee.full_name if r.enrolment and r.enrolment.trainee else None,
                "enrolment_id": r.enrolment_id,
                "name": r.name,
                "end_date": str(r.end_date),
                "days_remaining": (r.end_date - today).days,
            }
            for r in rotation_supervised
        ],
        "pending_validations": {
            "count": len(pending_logs),
            "oldest_days": oldest_days,
            "items": [
                {
                    "id": e.id,
                    "title": e.title,
                    "entry_type": e.entry_type,
                    "occurred_on": str(e.occurred_on),
                    "trainee_id": e.enrolment.trainee_id if e.enrolment else None,
                }
                for e in pending_logs[:10]
            ],
        },
        "draft_assessments": int(draft_assessments),
        "trainees_needing_support": [
            {
                "enrolment_id": e.id,
                "trainee": e.trainee.full_name if e.trainee else None,
                "rag": e.latest_rag,
                "overall_score": e.latest_overall_score,
                "year": e.current_year,
            }
            for e in at_risk
        ],
    }


@router.get("/dashboard/department", summary="Head of Department dashboard")
def department_dashboard(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    org_unit_id: str | None = None,
    period_days: int = Query(365, ge=30, le=1825),
):
    principal.require("analytics.department.read")
    since = date.today() - timedelta(days=period_days)

    org_ids: list[str] | None = None
    if org_unit_id:
        unit = db.get(OrgUnit, org_unit_id)
        if unit is None or unit.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Organisational unit not found.")
        descendants = db.execute(
            select(OrgUnit.id).where(
                OrgUnit.tenant_id == tenant_id, OrgUnit.path.like(f"{unit.path}/%")
            )
        ).scalars().all()
        org_ids = [unit.id, *descendants]

    enrol_stmt = select(Enrolment).where(
        Enrolment.tenant_id == tenant_id, Enrolment.deleted_at.is_(None)
    )
    if org_ids:
        enrol_stmt = enrol_stmt.where(Enrolment.org_unit_id.in_(org_ids))
    enrolments = list(db.execute(enrol_stmt).scalars().all())
    active = [e for e in enrolments if e.status == EnrolmentStatus.ACTIVE]
    enrolment_ids = [e.id for e in enrolments]

    rag_counts = Counter(e.latest_rag or RagStatus.UNKNOWN for e in active)
    by_year = Counter(e.current_year for e in active)
    by_level = Counter(e.current_level for e in active)

    log_stats = {"total": 0, "validated": 0, "pending": 0}
    research_count = publication_count = 0
    if enrolment_ids:
        log_rows = db.execute(
            select(LogEntry.validation_status, func.count())
            .where(
                LogEntry.enrolment_id.in_(enrolment_ids),
                LogEntry.deleted_at.is_(None),
                LogEntry.occurred_on >= since,
            )
            .group_by(LogEntry.validation_status)
        ).all()
        counts = {row[0]: row[1] for row in log_rows}
        log_stats = {
            "total": sum(counts.values()),
            "validated": counts.get(ValidationStatus.VALIDATED, 0),
            "pending": counts.get(ValidationStatus.PENDING, 0),
        }
        research_count = db.execute(
            select(func.count()).select_from(ResearchProject).where(
                ResearchProject.enrolment_id.in_(enrolment_ids),
                ResearchProject.deleted_at.is_(None),
            )
        ).scalar_one()
        publication_count = db.execute(
            select(func.count()).select_from(Publication).where(
                Publication.enrolment_id.in_(enrolment_ids),
                Publication.deleted_at.is_(None),
            )
        ).scalar_one()

    activity_stmt = select(func.count()).select_from(AcademicActivity).where(
        AcademicActivity.tenant_id == tenant_id,
        AcademicActivity.scheduled_on >= since,
        AcademicActivity.deleted_at.is_(None),
    )
    if org_ids:
        activity_stmt = activity_stmt.where(AcademicActivity.org_unit_id.in_(org_ids))
    activity_count = db.execute(activity_stmt).scalar_one()

    scores = [e.latest_overall_score for e in active if e.latest_overall_score is not None]

    return {
        "period_days": period_days,
        "trainees": {
            "total": len(enrolments),
            "active": len(active),
            "completed": sum(1 for e in enrolments if e.status == EnrolmentStatus.COMPLETED),
            "on_leave": sum(1 for e in enrolments if e.status == EnrolmentStatus.ON_LEAVE),
            "by_year": dict(sorted(by_year.items())),
            "by_level": dict(by_level),
        },
        "performance": {
            "rag": dict(rag_counts.items()),
            "mean_overall_score": round(sum(scores) / len(scores), 1) if scores else None,
            "promotion_ready": sum(1 for e in active if e.promotion_ready),
        },
        "logbook": log_stats,
        "academic_activities": int(activity_count),
        "research": {"projects": int(research_count), "publications": int(publication_count)},
        "at_risk": [
            {
                "enrolment_id": e.id,
                "trainee": e.trainee.full_name if e.trainee else None,
                "year": e.current_year,
                "rag": e.latest_rag,
                "score": e.latest_overall_score,
            }
            for e in active
            if e.latest_rag == RagStatus.RED
        ],
    }


@router.get("/dashboard/institution", summary="CMD / Director of Residency dashboard")
def institution_dashboard(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    period_days: int = Query(365, ge=30, le=1825),
):
    principal.require("analytics.institution.read")
    since = date.today() - timedelta(days=period_days)

    departments = db.execute(
        select(OrgUnit).where(
            OrgUnit.tenant_id == tenant_id,
            OrgUnit.kind == "department",
            OrgUnit.is_active.is_(True),
            OrgUnit.deleted_at.is_(None),
        ).order_by(OrgUnit.name)
    ).scalars().all()

    enrolments = db.execute(
        select(Enrolment).where(Enrolment.tenant_id == tenant_id, Enrolment.deleted_at.is_(None))
    ).scalars().all()
    active = [e for e in enrolments if e.status == EnrolmentStatus.ACTIVE]

    by_department: list[dict] = []
    for dept in departments:
        descendants = db.execute(
            select(OrgUnit.id).where(
                OrgUnit.tenant_id == tenant_id, OrgUnit.path.like(f"{dept.path}/%")
            )
        ).scalars().all()
        scope = {dept.id, *descendants}
        dept_enrolments = [e for e in active if e.org_unit_id in scope]
        scores = [e.latest_overall_score for e in dept_enrolments if e.latest_overall_score is not None]
        rag = Counter(e.latest_rag or RagStatus.UNKNOWN for e in dept_enrolments)
        by_department.append(
            {
                "org_unit_id": dept.id,
                "name": dept.name,
                "code": dept.code,
                "active_trainees": len(dept_enrolments),
                "mean_score": round(sum(scores) / len(scores), 1) if scores else None,
                "rag": dict(rag),
                "promotion_ready": sum(1 for e in dept_enrolments if e.promotion_ready),
            }
        )

    decisions = db.execute(
        select(PromotionReview.outcome, func.count())
        .where(PromotionReview.tenant_id == tenant_id, PromotionReview.review_date >= since)
        .group_by(PromotionReview.outcome)
    ).all()

    staff_count = db.execute(
        select(func.count()).select_from(User).where(
            User.tenant_id == tenant_id, User.status == "active", User.deleted_at.is_(None)
        )
    ).scalar_one()

    all_scores = [e.latest_overall_score for e in active if e.latest_overall_score is not None]

    return {
        "period_days": period_days,
        "headline": {
            "departments": len(departments),
            "active_trainees": len(active),
            "total_enrolments": len(enrolments),
            "completed": sum(1 for e in enrolments if e.status == EnrolmentStatus.COMPLETED),
            "active_staff": int(staff_count),
            "mean_overall_score": round(sum(all_scores) / len(all_scores), 1) if all_scores else None,
            "promotion_ready": sum(1 for e in active if e.promotion_ready),
        },
        "rag_distribution": dict(Counter(e.latest_rag or RagStatus.UNKNOWN for e in active)),
        "promotion_outcomes": {row[0] or "pending": row[1] for row in decisions},
        "departments": sorted(by_department, key=lambda d: -(d["active_trainees"] or 0)),
    }
