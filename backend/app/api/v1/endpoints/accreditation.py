"""Accreditation profiles, criteria and generated returns."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, TenantId
from app.db.base import owned_or_shared
from app.models.analytics import (
    AccreditationCriterion,
    AccreditationEvidence,
    AccreditationProfile,
    AccreditationReview,
)
from app.models.enums import AuditAction
from app.models.tenancy import OrgUnit
from app.services import accreditation as engine, audit

router = APIRouter()


@router.get("/metrics", summary="Measurable accreditation metrics")
def list_metrics(principal: CurrentPrincipal):
    """The vocabulary available when authoring criteria. A body's standard is expressed
    entirely in terms of these, which is why a new accrediting body needs no code."""
    return {
        "metrics": sorted(engine.METRICS.keys()),
        "operators": ["gte", "gt", "lte", "lt", "eq"],
        "weightings": ["essential", "desirable", "informational"],
        "notes": {
            "infrastructure": "Reads a declared figure from OrgUnit.capacity; set "
                              "parameters.capacity_key (e.g. 'icu_beds', 'operating_theatres').",
            "academic_activity_frequency": "Returns sessions per month; filter with "
                                           "parameters.activity_kinds.",
        },
    }


@router.get("/profiles", summary="List accreditation profiles")
def list_profiles(db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId,
                  body: str | None = None):
    stmt = select(AccreditationProfile).where(
        owned_or_shared(AccreditationProfile.tenant_id, tenant_id),
        AccreditationProfile.is_active.is_(True),
    )
    if body:
        stmt = stmt.where(AccreditationProfile.body == body)
    rows = db.execute(stmt.order_by(AccreditationProfile.body, AccreditationProfile.name)).scalars().all()
    return [
        {
            "id": p.id, "body": p.body, "body_name": p.body_name, "code": p.code,
            "name": p.name, "version": p.version, "criteria_count": len(p.criteria),
            "applies_to_programme_types": p.applies_to_programme_types,
            "effective_from": p.effective_from, "is_shared": p.tenant_id is None,
        }
        for p in rows
    ]


@router.get("/profiles/{profile_id}", summary="Read a profile with its criteria")
def get_profile(profile_id: str, db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    profile = db.get(AccreditationProfile, profile_id)
    if profile is None or profile.tenant_id not in (tenant_id, None):
        raise HTTPException(status_code=404, detail="Accreditation profile not found.")
    return {
        "id": profile.id, "body": profile.body, "body_name": profile.body_name,
        "code": profile.code, "name": profile.name, "version": profile.version,
        "description": profile.description,
        "criteria": [
            {
                "id": c.id, "section": c.section, "code": c.code, "title": c.title,
                "description": c.description, "metric": c.metric, "operator": c.operator,
                "target_value": c.target_value, "unit": c.unit, "weighting": c.weighting,
                "parameters": c.parameters, "evidence_guidance": c.evidence_guidance,
            }
            for c in profile.criteria
        ],
    }


@router.post("/profiles", status_code=status.HTTP_201_CREATED, summary="Create a profile")
def create_profile(payload: dict, db: DbSession, principal: CurrentPrincipal,
                   tenant_id: TenantId, meta: ClientMeta):
    principal.require("accreditation.profile.manage")
    required = {"body", "body_name", "code", "name"}
    if missing := required - payload.keys():
        raise HTTPException(status_code=422, detail=f"Missing field(s): {', '.join(sorted(missing))}.")

    profile = AccreditationProfile(
        tenant_id=tenant_id,
        body=payload["body"],
        body_name=payload["body_name"],
        code=payload["code"],
        name=payload["name"],
        version=payload.get("version", "1.0"),
        description=payload.get("description"),
        applies_to_programme_types=payload.get("applies_to_programme_types", []),
        effective_from=payload.get("effective_from"),
        report_template=payload.get("report_template", {}),
    )
    db.add(profile)
    db.flush()

    for index, criterion in enumerate(payload.get("criteria", [])):
        if criterion.get("metric") not in engine.METRICS:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown metric '{criterion.get('metric')}'. "
                       f"Valid metrics: {', '.join(sorted(engine.METRICS))}.",
            )
        db.add(
            AccreditationCriterion(
                tenant_id=tenant_id,
                profile_id=profile.id,
                section=criterion.get("section", "general"),
                code=criterion.get("code", f"C{index + 1}"),
                title=criterion["title"],
                description=criterion.get("description"),
                metric=criterion["metric"],
                operator=criterion.get("operator", "gte"),
                target_value=float(criterion.get("target_value", 0)),
                unit=criterion.get("unit"),
                parameters=criterion.get("parameters", {}),
                weighting=criterion.get("weighting", "essential"),
                evidence_guidance=criterion.get("evidence_guidance"),
                sort_order=index,
            )
        )

    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="accreditation_profile", entity_id=profile.id,
                 summary=f"Created accreditation profile '{profile.name}'", **meta)
    db.commit()
    db.refresh(profile)
    return {"id": profile.id, "criteria": len(profile.criteria)}


@router.post("/reviews", summary="Generate an accreditation return")
def generate_review(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    org_unit_id: str = Query(...),
    profile_id: str = Query(...),
    period_start: date | None = None,
    period_end: date | None = None,
    persist: bool = True,
):
    """Evaluate a department against a body's standard and produce the return, with a
    plain-language narrative and a ranked gap list."""
    principal.require("accreditation.report.generate")
    unit = db.get(OrgUnit, org_unit_id)
    if unit is None or unit.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Organisational unit not found.")
    profile = db.get(AccreditationProfile, profile_id)
    if profile is None or profile.tenant_id not in (tenant_id, None):
        raise HTTPException(status_code=404, detail="Accreditation profile not found.")

    end = period_end or date.today()
    start = period_start or (end - timedelta(days=365))

    review, results = engine.generate_review(
        db, org_unit=unit, profile=profile, period_start=start, period_end=end,
        generated_by_id=principal.id, persist=persist,
    )
    if persist:
        audit.record(db, action=AuditAction.EXPORT, tenant_id=tenant_id, actor_id=principal.id,
                     entity_type="accreditation_review", entity_id=review.id,
                     summary=f"Generated {profile.body_name} return for {unit.name}", **meta)
        db.commit()
        db.refresh(review)

    return {
        "review_id": review.id if persist else None,
        "org_unit": {"id": unit.id, "name": unit.name, "code": unit.code},
        "profile": {"id": profile.id, "body": profile.body, "name": profile.name,
                    "version": profile.version},
        "period": {"start": str(start), "end": str(end)},
        "compliance_percent": round(review.compliance_percent, 1),
        "essential_met": review.essential_met,
        "essential_total": review.essential_total,
        "readiness_rag": review.readiness_rag,
        "criteria": review.criterion_results,
        "gaps": review.gaps,
        "narrative": review.narrative,
    }


@router.get("/reviews", summary="Past accreditation returns")
def list_reviews(db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId,
                 org_unit_id: str | None = None):
    principal.require("accreditation.report.generate")
    stmt = select(AccreditationReview).where(AccreditationReview.tenant_id == tenant_id)
    if org_unit_id:
        stmt = stmt.where(AccreditationReview.org_unit_id == org_unit_id)
    rows = db.execute(stmt.order_by(AccreditationReview.generated_at.desc()).limit(100)).scalars().all()
    return [
        {
            "id": r.id, "org_unit_id": r.org_unit_id, "profile_id": r.profile_id,
            "period_start": r.period_start, "period_end": r.period_end,
            "generated_at": r.generated_at, "compliance_percent": round(r.compliance_percent, 1),
            "readiness_rag": r.readiness_rag, "gap_count": len(r.gaps or []),
        }
        for r in rows
    ]


@router.post("/evidence", status_code=status.HTTP_201_CREATED, summary="Attach evidence")
def add_evidence(payload: dict, db: DbSession, principal: CurrentPrincipal,
                 tenant_id: TenantId, meta: ClientMeta):
    principal.require("accreditation.evidence.manage")
    if not payload.get("title") or not payload.get("org_unit_id"):
        raise HTTPException(status_code=422, detail="'title' and 'org_unit_id' are required.")
    evidence = AccreditationEvidence(
        tenant_id=tenant_id,
        org_unit_id=payload["org_unit_id"],
        criterion_id=payload.get("criterion_id"),
        title=payload["title"],
        description=payload.get("description"),
        object_key=payload.get("object_key"),
        valid_from=payload.get("valid_from"),
        valid_to=payload.get("valid_to"),
        uploaded_by_id=principal.id,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return {"id": evidence.id, "title": evidence.title}
