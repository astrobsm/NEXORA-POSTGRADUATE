"""Workplace-based assessment: instrument design, submission and competency ratings."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, TenantId
from app.db.base import owned_or_shared, utcnow
from app.models.assessment import Assessment, AssessmentTemplate, CompetencyRating
from app.models.curriculum import Competency
from app.models.enums import (
    ENTRUSTMENT_ORDER,
    ApprovalStatus,
    AssessmentVerdict,
    AuditAction,
)
from app.models.training import Enrolment
from app.schemas.common import Page
from app.schemas.training import (
    AssessmentCreate,
    AssessmentOut,
    AssessmentTemplateCreate,
    AssessmentTemplateOut,
    CompetencyRatingOut,
)
from app.services import audit

router = APIRouter()


# --------------------------------------------------------------------------
# Scoring a dynamic form
# --------------------------------------------------------------------------
def score_responses(
    template: AssessmentTemplate, responses: dict[str, Any]
) -> tuple[float | None, float | None, float | None, str | None, bool | None]:
    """Score a submission against its template's declarative schema.

    Only fields of type ``scale`` or ``numeric`` contribute. Each carries an optional
    weight; ``not_applicable`` responses are excluded from both numerator and
    denominator so a partially-relevant encounter is not penalised.
    """
    config = template.scoring_config or {}
    scale_max = float(config.get("scale_max", 9))
    pass_mark = float(config.get("pass_mark", 60))

    numerator = 0.0
    denominator = 0.0

    for field in template.form_schema or []:
        if field.get("type") not in {"scale", "numeric"}:
            continue
        key = field.get("key")
        if key is None:
            continue
        value = responses.get(key)
        if value in (None, "", "n/a", "not_applicable"):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        weight = float(field.get("weight", 1.0))
        field_max = float(field.get("max", scale_max))
        numerator += numeric * weight
        denominator += field_max * weight

    if denominator == 0:
        return None, None, None, None, None

    percent = numerator / denominator * 100
    bands = config.get("verdict_bands") or {
        AssessmentVerdict.BELOW_EXPECTATION: 40,
        AssessmentVerdict.BORDERLINE: 55,
        AssessmentVerdict.MEETS_EXPECTATION: 70,
        AssessmentVerdict.ABOVE_EXPECTATION: 85,
        AssessmentVerdict.OUTSTANDING: 95,
    }
    verdict = AssessmentVerdict.BELOW_EXPECTATION
    for name, floor in sorted(bands.items(), key=lambda kv: kv[1]):
        if percent >= float(floor):
            verdict = name
    return numerator, denominator, percent, verdict, percent >= pass_mark


def _out(db, a: Assessment) -> AssessmentOut:
    out = AssessmentOut.model_validate(a)
    if a.template is not None:
        out.template_name = a.template.name
        out.template_kind = a.template.kind
    if a.assessor is not None:
        out.assessor_name = a.assessor.full_name
    return out


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------
@router.get("/templates", response_model=list[AssessmentTemplateOut],
            summary="List assessment instruments")
def list_templates(db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId,
                   kind: str | None = None, org_unit_id: str | None = None):
    stmt = select(AssessmentTemplate).where(
        AssessmentTemplate.tenant_id == tenant_id,
        AssessmentTemplate.deleted_at.is_(None),
        AssessmentTemplate.is_active.is_(True),
    )
    if kind:
        stmt = stmt.where(AssessmentTemplate.kind == kind)
    if org_unit_id:
        stmt = stmt.where(
            owned_or_shared(AssessmentTemplate.org_unit_id, org_unit_id)
        )
    rows = db.execute(stmt.order_by(AssessmentTemplate.name)).scalars().all()
    return [AssessmentTemplateOut.model_validate(t) for t in rows]


@router.post("/templates", response_model=AssessmentTemplateOut,
             status_code=status.HTTP_201_CREATED, summary="Design an assessment instrument")
def create_template(payload: AssessmentTemplateCreate, db: DbSession,
                    principal: CurrentPrincipal, tenant_id: TenantId, meta: ClientMeta):
    """Create a form the client renders dynamically — no frontend change is needed for
    a department to invent its own instrument."""
    principal.require("assessment.template.manage")
    if db.execute(
        select(AssessmentTemplate).where(
            AssessmentTemplate.tenant_id == tenant_id, AssessmentTemplate.code == payload.code
        )
    ).scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Instrument code '{payload.code}' already exists.")

    for field in payload.form_schema:
        if "key" not in field or "type" not in field:
            raise HTTPException(
                status_code=422,
                detail="Every form field needs a 'key' and a 'type'. "
                       "Supported types: scale, numeric, text, textarea, select, checkbox, date.",
            )

    template = AssessmentTemplate(tenant_id=tenant_id, **payload.model_dump())
    db.add(template)
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="assessment_template", entity_id=template.id,
                 summary=f"Created instrument '{payload.name}'", **meta)
    db.commit()
    db.refresh(template)
    return AssessmentTemplateOut.model_validate(template)


# --------------------------------------------------------------------------
# Assessments
# --------------------------------------------------------------------------
@router.post("", response_model=AssessmentOut, status_code=status.HTTP_201_CREATED,
             summary="Record an assessment")
def create_assessment(payload: AssessmentCreate, db: DbSession, principal: CurrentPrincipal,
                      tenant_id: TenantId, meta: ClientMeta):
    principal.require("assessment.submit")
    template = db.get(AssessmentTemplate, payload.template_id)
    if template is None or template.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Assessment instrument not found.")
    enrolment = db.get(Enrolment, payload.enrolment_id)
    if enrolment is None or enrolment.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Enrolment not found.")
    if enrolment.trainee_id == principal.id:
        raise HTTPException(status_code=403, detail="You cannot assess yourself.")

    raw, maximum, percent, verdict, is_pass = score_responses(template, payload.responses)

    assessment = Assessment(
        tenant_id=tenant_id,
        template_id=template.id,
        enrolment_id=enrolment.id,
        rotation_assignment_id=payload.rotation_assignment_id,
        assessor_id=principal.id,
        occurred_on=payload.occurred_on,
        setting=payload.setting,
        case_summary=payload.case_summary,
        case_complexity=payload.case_complexity,
        responses=payload.responses,
        raw_score=raw,
        max_score=maximum,
        percent_score=percent,
        verdict=verdict,
        is_pass=is_pass,
        strengths=payload.strengths,
        development_needs=payload.development_needs,
        agreed_actions=payload.agreed_actions,
        status=ApprovalStatus.APPROVED if payload.submit else ApprovalStatus.DRAFT,
        submitted_at=utcnow() if payload.submit else None,
    )
    db.add(assessment)
    db.flush()

    for rating in payload.competency_ratings:
        competency_id, level = rating.get("competency_id"), rating.get("level")
        if not competency_id or not level:
            continue
        competency = db.get(Competency, competency_id)
        if competency is None:
            continue
        db.add(
            CompetencyRating(
                tenant_id=tenant_id,
                enrolment_id=enrolment.id,
                competency_id=competency_id,
                assessment_id=assessment.id,
                rotation_assignment_id=payload.rotation_assignment_id,
                assessor_id=principal.id,
                level=level,
                level_value=ENTRUSTMENT_ORDER.get(level, 2),
                rated_on=payload.occurred_on,
                evidence=rating.get("evidence"),
            )
        )

    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="assessment", entity_id=assessment.id,
                 summary=f"{template.name} recorded for enrolment {enrolment.id}", **meta)
    db.commit()
    db.refresh(assessment)
    return _out(db, assessment)


@router.get("", response_model=Page[AssessmentOut], summary="List assessments")
def list_assessments(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    enrolment_id: str | None = None,
    assessor_id: str | None = None,
    kind: str | None = None,
    assessment_status: str | None = Query(default=None, alias="status"),
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    stmt = select(Assessment).where(
        Assessment.tenant_id == tenant_id, Assessment.deleted_at.is_(None)
    )
    if not principal.is_superuser and not principal.has("assessment.read.any"):
        own = select(Enrolment.id).where(Enrolment.trainee_id == principal.id)
        stmt = stmt.where(
            Assessment.enrolment_id.in_(own) | (Assessment.assessor_id == principal.id)
        )
    if enrolment_id:
        stmt = stmt.where(Assessment.enrolment_id == enrolment_id)
    if assessor_id:
        stmt = stmt.where(Assessment.assessor_id == assessor_id)
    if assessment_status:
        stmt = stmt.where(Assessment.status == assessment_status)
    if kind:
        stmt = stmt.join(AssessmentTemplate, AssessmentTemplate.id == Assessment.template_id).where(
            AssessmentTemplate.kind == kind
        )
    if date_from:
        stmt = stmt.where(Assessment.occurred_on >= date_from)
    if date_to:
        stmt = stmt.where(Assessment.occurred_on <= date_to)

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(Assessment.occurred_on.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return Page[AssessmentOut](
        items=[_out(db, a) for a in rows], total=total, page=page, page_size=page_size
    )


@router.post("/{assessment_id}/reflection", response_model=AssessmentOut,
             summary="Add the trainee's reflection")
def add_reflection(assessment_id: str, db: DbSession, principal: CurrentPrincipal,
                   tenant_id: TenantId, reflection: str = Query(min_length=1)):
    assessment = db.get(Assessment, assessment_id)
    if assessment is None or assessment.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Assessment not found.")
    enrolment = db.get(Enrolment, assessment.enrolment_id)
    if enrolment.trainee_id != principal.id:
        raise HTTPException(status_code=403, detail="Only the trainee can add their reflection.")
    assessment.trainee_reflection = reflection
    assessment.trainee_acknowledged_at = utcnow()
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    return _out(db, assessment)


# --------------------------------------------------------------------------
# Competency ratings
# --------------------------------------------------------------------------
@router.get("/competency-ratings/{enrolment_id}", response_model=list[CompetencyRatingOut],
            summary="Latest entrustment level per competency")
def competency_ratings(enrolment_id: str, db: DbSession, principal: CurrentPrincipal,
                       tenant_id: TenantId, latest_only: bool = True):
    enrolment = db.get(Enrolment, enrolment_id)
    if enrolment is None or enrolment.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Enrolment not found.")
    if enrolment.trainee_id != principal.id:
        principal.require("assessment.read.any", org_unit_id=enrolment.org_unit_id)

    rows = db.execute(
        select(CompetencyRating)
        .where(CompetencyRating.enrolment_id == enrolment_id)
        .order_by(CompetencyRating.rated_on.asc())
    ).scalars().all()

    if latest_only:
        latest: dict[str, CompetencyRating] = {}
        for row in rows:
            latest[row.competency_id] = row
        rows = list(latest.values())

    out = []
    for row in rows:
        item = CompetencyRatingOut.model_validate(row)
        if row.competency is not None:
            item.competency_code = row.competency.code
            item.competency_title = row.competency.title
        out.append(item)
    return out


@router.get("/competency-progress/{enrolment_id}", summary="Competency attainment vs. target")
def competency_progress(enrolment_id: str, db: DbSession, principal: CurrentPrincipal,
                        tenant_id: TenantId):
    """Every competency in the curriculum with its current level, this year's target and
    the gap — the view a supervisor uses in an educational review."""
    enrolment = db.get(Enrolment, enrolment_id)
    if enrolment is None or enrolment.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Enrolment not found.")
    if enrolment.trainee_id != principal.id:
        principal.require("assessment.read.any", org_unit_id=enrolment.org_unit_id)

    competencies = db.execute(
        select(Competency)
        .where(Competency.curriculum_version_id == enrolment.curriculum_version_id)
        .order_by(Competency.domain, Competency.sort_order, Competency.code)
    ).scalars().all()

    ratings = db.execute(
        select(CompetencyRating)
        .where(
            CompetencyRating.enrolment_id == enrolment_id,
            CompetencyRating.is_self_rating.is_(False),
        )
        .order_by(CompetencyRating.rated_on.asc())
    ).scalars().all()
    current: dict[str, CompetencyRating] = {r.competency_id: r for r in ratings}

    year_key = str(enrolment.current_year)
    items = []
    for c in competencies:
        target_level = (c.target_by_year or {}).get(year_key, c.exit_target)
        target_value = ENTRUSTMENT_ORDER.get(target_level, 4)
        rating = current.get(c.id)
        achieved = rating.level_value if rating else 0
        items.append(
            {
                "competency_id": c.id,
                "code": c.code,
                "title": c.title,
                "domain": c.domain,
                "is_epa": c.is_epa,
                "target_level": target_level,
                "target_value": target_value,
                "current_level": rating.level if rating else None,
                "current_value": achieved,
                "rated_on": rating.rated_on if rating else None,
                "met": achieved >= target_value,
                "gap": max(0, target_value - achieved),
            }
        )

    met = sum(1 for i in items if i["met"])
    return {
        "enrolment_id": enrolment_id,
        "training_year": enrolment.current_year,
        "total": len(items),
        "met": met,
        "unrated": sum(1 for i in items if i["current_value"] == 0),
        "percent_met": round(met / len(items) * 100, 1) if items else 0.0,
        "by_domain": {
            domain: {
                "total": sum(1 for i in items if i["domain"] == domain),
                "met": sum(1 for i in items if i["domain"] == domain and i["met"]),
            }
            for domain in sorted({i["domain"] for i in items})
        },
        "competencies": items,
    }
