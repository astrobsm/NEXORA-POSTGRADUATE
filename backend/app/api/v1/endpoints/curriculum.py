"""The curriculum builder: specialties, programmes, versions, years, rotations,
competencies, requirement rules and the procedure catalogue.

Everything a department needs to define its own training programme, without any
software change.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, TenantId
from app.db.base import owned_or_shared
from app.models.curriculum import (
    Competency,
    CurriculumVersion,
    ProcedureCatalogueItem,
    Programme,
    RequirementRule,
    RotationTemplate,
    Specialty,
    TrainingYear,
)
from app.models.enums import (
    AuditAction,
    CurriculumStatus,
    RequirementKind,
    RequirementOperator,
    RequirementScope,
    RequirementSeverity,
    ScoreDomain,
)
from app.models.tenancy import OrgUnit
from app.schemas.training import (
    CompetencyCreate,
    CompetencyOut,
    CurriculumVersionOut,
    ProgrammeCreate,
    ProgrammeOut,
    RequirementRuleCreate,
    RequirementRuleOut,
    RotationTemplateCreate,
    RotationTemplateOut,
    SpecialtyCreate,
    SpecialtyOut,
    TrainingYearCreate,
    TrainingYearOut,
)
from app.services import audit
from app.services.requirements import MEASURERS

router = APIRouter()


# --------------------------------------------------------------------------
# Specialties
# --------------------------------------------------------------------------
@router.get("/specialties", response_model=list[SpecialtyOut], summary="List specialties")
def list_specialties(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    faculty_group: str | None = None,
    discipline: str | None = None,
    include_subspecialties: bool = True,
):
    stmt = select(Specialty).where(
        owned_or_shared(Specialty.tenant_id, tenant_id),
        Specialty.deleted_at.is_(None),
        Specialty.is_active.is_(True),
    )
    if faculty_group:
        stmt = stmt.where(Specialty.faculty_group == faculty_group)
    if discipline:
        stmt = stmt.where(Specialty.discipline == discipline)
    if not include_subspecialties:
        stmt = stmt.where(Specialty.is_subspecialty.is_(False))
    rows = db.execute(
        stmt.order_by(Specialty.faculty_group, Specialty.sort_order, Specialty.name)
    ).scalars().all()
    return [SpecialtyOut.model_validate(s) for s in rows]


@router.post("/specialties", response_model=SpecialtyOut, status_code=status.HTTP_201_CREATED,
             summary="Create a specialty or subspecialty")
def create_specialty(
    payload: SpecialtyCreate,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    """Any future specialty is creatable here — no code change is ever required to add
    one, including subspecialties nested beneath an existing parent."""
    principal.require("curriculum.specialty.manage")
    if db.execute(
        select(Specialty).where(Specialty.tenant_id == tenant_id, Specialty.code == payload.code)
    ).scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Specialty code '{payload.code}' already exists.")

    parent = db.get(Specialty, payload.parent_id) if payload.parent_id else None
    specialty = Specialty(
        tenant_id=tenant_id,
        parent_id=payload.parent_id,
        code=payload.code,
        name=payload.name,
        faculty_group=payload.faculty_group or (parent.faculty_group if parent else None),
        discipline=payload.discipline,
        recognised_by=payload.recognised_by,
        description=payload.description,
        is_subspecialty=parent is not None,
    )
    db.add(specialty)
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="specialty", entity_id=specialty.id,
                 summary=f"Created specialty '{payload.name}'", **meta)
    db.commit()
    db.refresh(specialty)
    return SpecialtyOut.model_validate(specialty)


# --------------------------------------------------------------------------
# Programmes
# --------------------------------------------------------------------------
@router.get("/programmes", response_model=list[ProgrammeOut], summary="List programmes")
def list_programmes(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    org_unit_id: str | None = None,
    programme_type: str | None = None,
    awarding_body: str | None = None,
):
    principal.require("curriculum.read")
    stmt = select(Programme).where(
        Programme.tenant_id == tenant_id, Programme.deleted_at.is_(None)
    )
    if org_unit_id:
        stmt = stmt.where(Programme.org_unit_id == org_unit_id)
    if programme_type:
        stmt = stmt.where(Programme.programme_type == programme_type)
    if awarding_body:
        stmt = stmt.where(Programme.awarding_body == awarding_body)
    rows = db.execute(stmt.order_by(Programme.name)).scalars().all()
    return [ProgrammeOut.model_validate(p) for p in rows]


@router.post("/programmes", response_model=ProgrammeOut, status_code=status.HTTP_201_CREATED,
             summary="Create a programme")
def create_programme(
    payload: ProgrammeCreate,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    principal.require("curriculum.manage")
    unit = db.get(OrgUnit, payload.org_unit_id)
    if unit is None or unit.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Organisational unit not found.")
    if db.execute(
        select(Programme).where(Programme.tenant_id == tenant_id, Programme.code == payload.code)
    ).scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Programme code '{payload.code}' already exists.")

    programme = Programme(tenant_id=tenant_id, **payload.model_dump())
    db.add(programme)
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="programme", entity_id=programme.id,
                 summary=f"Created programme '{payload.name}'", **meta)
    db.commit()
    db.refresh(programme)
    return ProgrammeOut.model_validate(programme)


@router.get("/programmes/{programme_id}/versions", response_model=list[dict],
            summary="Curriculum versions of a programme")
def list_versions(programme_id: str, db: DbSession, principal: CurrentPrincipal,
                  tenant_id: TenantId):
    principal.require("curriculum.read")
    rows = db.execute(
        select(CurriculumVersion)
        .where(
            CurriculumVersion.programme_id == programme_id,
            CurriculumVersion.tenant_id == tenant_id,
        )
        .order_by(CurriculumVersion.created_at.desc())
    ).scalars().all()
    return [
        {
            "id": v.id,
            "version": v.version,
            "title": v.title,
            "status": v.status,
            "effective_from": v.effective_from,
            "effective_to": v.effective_to,
            "training_years": len(v.training_years),
            "competencies": len(v.competencies),
            "requirements": len(v.requirements),
        }
        for v in rows
    ]


@router.post("/programmes/{programme_id}/versions", response_model=CurriculumVersionOut,
             status_code=status.HTTP_201_CREATED, summary="Start a new curriculum version")
def create_version(
    programme_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    version: str = Query(...),
    title: str = Query(...),
    copy_from_id: str | None = None,
):
    """Create a draft version, optionally cloning an existing one so a revision starts
    from what is already in force rather than a blank page."""
    principal.require("curriculum.manage")
    programme = db.get(Programme, programme_id)
    if programme is None or programme.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Programme not found.")

    new_version = CurriculumVersion(
        tenant_id=tenant_id, programme_id=programme_id, version=version, title=title,
        status=CurriculumStatus.DRAFT,
    )
    db.add(new_version)
    db.flush()

    if copy_from_id:
        source = db.get(CurriculumVersion, copy_from_id)
        if source is None or source.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Source curriculum version not found.")
        new_version.aims = source.aims
        new_version.score_weights = dict(source.score_weights or {})
        new_version.resources = list(source.resources or [])

        year_map: dict[str, str] = {}
        template_map: dict[str, str] = {}
        competency_map: dict[str, str] = {}

        for year in source.training_years:
            clone = TrainingYear(
                tenant_id=tenant_id, curriculum_version_id=new_version.id,
                sequence=year.sequence, name=year.name, level=year.level,
                duration_months=year.duration_months, objectives=list(year.objectives or []),
                expectations=dict(year.expectations or {}),
            )
            db.add(clone)
            db.flush()
            year_map[year.id] = clone.id
            for template in year.rotations:
                t_clone = RotationTemplate(
                    tenant_id=tenant_id, training_year_id=clone.id,
                    org_unit_id=template.org_unit_id, specialty_id=template.specialty_id,
                    name=template.name, code=template.code, sequence=template.sequence,
                    duration_weeks=template.duration_weeks, is_elective=template.is_elective,
                    is_mandatory=template.is_mandatory, max_trainees=template.max_trainees,
                    objectives=list(template.objectives or []),
                    required_assessments=list(template.required_assessments or []),
                    description=template.description,
                )
                db.add(t_clone)
                db.flush()
                template_map[template.id] = t_clone.id

        for comp in source.competencies:
            c_clone = Competency(
                tenant_id=tenant_id, curriculum_version_id=new_version.id, code=comp.code,
                title=comp.title, description=comp.description, domain=comp.domain,
                is_epa=comp.is_epa, target_by_year=dict(comp.target_by_year or {}),
                exit_target=comp.exit_target, weight=comp.weight,
                assessment_methods=list(comp.assessment_methods or []),
                sort_order=comp.sort_order,
            )
            db.add(c_clone)
            db.flush()
            competency_map[comp.id] = c_clone.id

        for rule in source.requirements:
            db.add(
                RequirementRule(
                    tenant_id=tenant_id, curriculum_version_id=new_version.id,
                    training_year_id=year_map.get(rule.training_year_id),
                    rotation_template_id=template_map.get(rule.rotation_template_id),
                    competency_id=competency_map.get(rule.competency_id),
                    code=rule.code, label=rule.label, kind=rule.kind, operator=rule.operator,
                    target_value=rule.target_value, parameters=dict(rule.parameters or {}),
                    scope=rule.scope, severity=rule.severity, weight=rule.weight,
                    score_domain=rule.score_domain, guidance=rule.guidance,
                    source_reference=rule.source_reference,
                )
            )
        new_version.change_notes = f"Cloned from version {source.version}."

    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="curriculum_version", entity_id=new_version.id,
                 summary=f"Created curriculum version {version}", **meta)
    db.commit()
    db.refresh(new_version)
    return CurriculumVersionOut.model_validate(new_version)


@router.get("/versions/{version_id}", response_model=CurriculumVersionOut,
            summary="Read a curriculum version in full")
def get_version(version_id: str, db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    principal.require("curriculum.read")
    version = db.get(CurriculumVersion, version_id)
    if version is None or version.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Curriculum version not found.")
    return CurriculumVersionOut.model_validate(version)


@router.post("/versions/{version_id}/publish", summary="Publish a curriculum version")
def publish_version(
    version_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    effective_from: date | None = None,
):
    """Publishing supersedes the previously active version. Trainees already enrolled
    stay pinned to the version they started under."""
    principal.require("curriculum.publish")
    version = db.get(CurriculumVersion, version_id)
    if version is None or version.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Curriculum version not found.")
    if not version.training_years:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A curriculum cannot be published without at least one training year.",
        )

    for other in db.execute(
        select(CurriculumVersion).where(
            CurriculumVersion.programme_id == version.programme_id,
            CurriculumVersion.status == CurriculumStatus.ACTIVE,
            CurriculumVersion.id != version.id,
        )
    ).scalars():
        other.status = CurriculumStatus.SUPERSEDED
        other.effective_to = effective_from or date.today()
        db.add(other)

    version.status = CurriculumStatus.ACTIVE
    version.effective_from = effective_from or date.today()
    version.approved_by_id = principal.id
    version.approved_on = date.today()
    db.add(version)

    audit.record(db, action=AuditAction.APPROVE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="curriculum_version", entity_id=version.id,
                 summary=f"Published curriculum version {version.version}", **meta)
    db.commit()
    return {"detail": f"Version {version.version} is now active.",
            "effective_from": version.effective_from}


# --------------------------------------------------------------------------
# Training years, rotations, competencies
# --------------------------------------------------------------------------
@router.post("/versions/{version_id}/years", response_model=TrainingYearOut,
             status_code=status.HTTP_201_CREATED, summary="Add a training year")
def add_year(version_id: str, payload: TrainingYearCreate, db: DbSession,
             principal: CurrentPrincipal, tenant_id: TenantId, meta: ClientMeta):
    principal.require("curriculum.manage")
    version = db.get(CurriculumVersion, version_id)
    if version is None or version.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Curriculum version not found.")
    if version.status == CurriculumStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An active curriculum cannot be edited. Create a new version instead.",
        )
    year = TrainingYear(tenant_id=tenant_id, curriculum_version_id=version_id, **payload.model_dump())
    db.add(year)
    db.commit()
    db.refresh(year)
    return TrainingYearOut.model_validate(year)


@router.post("/rotation-templates", response_model=RotationTemplateOut,
             status_code=status.HTTP_201_CREATED, summary="Add a rotation to a training year")
def add_rotation_template(payload: RotationTemplateCreate, db: DbSession,
                          principal: CurrentPrincipal, tenant_id: TenantId, meta: ClientMeta):
    principal.require("curriculum.manage")
    year = db.get(TrainingYear, payload.training_year_id)
    if year is None or year.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Training year not found.")
    template = RotationTemplate(tenant_id=tenant_id, **payload.model_dump())
    db.add(template)
    db.commit()
    db.refresh(template)
    return RotationTemplateOut.model_validate(template)


@router.post("/versions/{version_id}/competencies", response_model=CompetencyOut,
             status_code=status.HTTP_201_CREATED, summary="Add a competency or EPA")
def add_competency(version_id: str, payload: CompetencyCreate, db: DbSession,
                   principal: CurrentPrincipal, tenant_id: TenantId, meta: ClientMeta):
    principal.require("curriculum.manage")
    version = db.get(CurriculumVersion, version_id)
    if version is None or version.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Curriculum version not found.")
    competency = Competency(
        tenant_id=tenant_id, curriculum_version_id=version_id, **payload.model_dump()
    )
    db.add(competency)
    db.commit()
    db.refresh(competency)
    return CompetencyOut.model_validate(competency)


@router.get("/versions/{version_id}/competencies", response_model=list[CompetencyOut],
            summary="List competencies")
def list_competencies(version_id: str, db: DbSession, principal: CurrentPrincipal,
                      tenant_id: TenantId, epas_only: bool = False):
    principal.require("curriculum.read")
    stmt = select(Competency).where(
        Competency.curriculum_version_id == version_id, Competency.tenant_id == tenant_id
    )
    if epas_only:
        stmt = stmt.where(Competency.is_epa.is_(True))
    rows = db.execute(stmt.order_by(Competency.domain, Competency.sort_order, Competency.code)).scalars().all()
    return [CompetencyOut.model_validate(c) for c in rows]


# --------------------------------------------------------------------------
# Requirement rules — the configurable policy layer
# --------------------------------------------------------------------------
@router.get("/requirement-kinds", summary="Requirement vocabulary for the rule builder")
def requirement_vocabulary(principal: CurrentPrincipal):
    """Everything the rule builder needs to render its form: which measurements exist,
    which operators apply, which scopes and severities are valid, and the parameters
    each measurement understands."""
    parameter_hints: dict[str, list[str]] = {
        RequirementKind.PROCEDURE_COUNT: ["entry_types", "grade", "procedure_ids", "complexities"],
        RequirementKind.PROCEDURE_ROLE_COUNT: ["role", "roles", "grade", "weighted", "procedure_ids"],
        RequirementKind.LOGBOOK_ENTRY_COUNT: ["entry_types", "org_unit_ids"],
        RequirementKind.COMPETENCY_LEVEL: ["competency_codes", "domains", "level", "aggregate"],
        RequirementKind.EPA_LEVEL: ["competency_codes", "level", "aggregate"],
        RequirementKind.ACADEMIC_ATTENDANCE_PCT: ["activity_kinds", "mandatory_only", "org_unit_ids"],
        RequirementKind.DUTY_ATTENDANCE_PCT: ["duty_kinds"],
        RequirementKind.ACTIVITY_PRESENTATION_COUNT: ["activity_kinds", "roles", "include_conferences"],
        RequirementKind.ASSESSMENT_PASS_COUNT: ["assessment_kinds", "template_codes"],
        RequirementKind.ASSESSMENT_MEAN_SCORE: ["assessment_kinds", "template_codes"],
        RequirementKind.EXAM_PASS: ["paper_ids"],
        RequirementKind.CME_CREDITS: ["recognised_by"],
        RequirementKind.RESEARCH_OUTPUT: ["min_stage", "research_types"],
        RequirementKind.PUBLICATION_COUNT: [
            "publication_types", "indexed_in", "peer_reviewed_only", "max_author_position",
        ],
        RequirementKind.DISSERTATION_STAGE: ["stage"],
        RequirementKind.ROTATION_COMPLETION: ["training_year"],
        RequirementKind.TEACHING_HOURS: [],
        RequirementKind.CUSTOM_EXPRESSION: ["expression", "inputs"],
    }
    return {
        "kinds": [
            {
                "kind": kind,
                "implemented": kind in MEASURERS,
                "parameters": parameter_hints.get(kind, []),
            }
            for kind in (k.value for k in RequirementKind)
        ],
        "operators": [o.value for o in RequirementOperator],
        "scopes": [s.value for s in RequirementScope],
        "severities": [s.value for s in RequirementSeverity],
        "score_domains": [d.value for d in ScoreDomain],
    }


@router.get("/versions/{version_id}/requirements", response_model=list[RequirementRuleOut],
            summary="List requirement rules")
def list_requirements(version_id: str, db: DbSession, principal: CurrentPrincipal,
                      tenant_id: TenantId, scope: str | None = None):
    principal.require("curriculum.read")
    stmt = select(RequirementRule).where(
        RequirementRule.curriculum_version_id == version_id,
        RequirementRule.tenant_id == tenant_id,
    )
    if scope:
        stmt = stmt.where(RequirementRule.scope == scope)
    rows = db.execute(stmt.order_by(RequirementRule.scope, RequirementRule.label)).scalars().all()
    return [RequirementRuleOut.model_validate(r) for r in rows]


@router.post("/versions/{version_id}/requirements", response_model=RequirementRuleOut,
             status_code=status.HTTP_201_CREATED, summary="Add a requirement rule")
def add_requirement(
    version_id: str,
    payload: RequirementRuleCreate,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    """Define institutional or college policy as data. The rule is validated against the
    engine's vocabulary so an unmeasurable rule can never be saved."""
    principal.require("curriculum.manage")
    version = db.get(CurriculumVersion, version_id)
    if version is None or version.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Curriculum version not found.")
    if payload.kind not in MEASURERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{payload.kind}' is not a measurable requirement kind. "
                   f"Valid kinds: {', '.join(sorted(MEASURERS))}.",
        )
    if payload.operator not in {o.value for o in RequirementOperator}:
        raise HTTPException(status_code=422, detail=f"Unknown operator '{payload.operator}'.")

    rule = RequirementRule(
        tenant_id=tenant_id, curriculum_version_id=version_id, **payload.model_dump()
    )
    db.add(rule)
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="requirement_rule", entity_id=rule.id,
                 summary=f"Added requirement '{payload.label}'", **meta)
    db.commit()
    db.refresh(rule)
    return RequirementRuleOut.model_validate(rule)


@router.patch("/requirements/{rule_id}", response_model=RequirementRuleOut,
              summary="Update a requirement rule")
def update_requirement(rule_id: str, payload: dict, db: DbSession, principal: CurrentPrincipal,
                       tenant_id: TenantId, meta: ClientMeta):
    principal.require("curriculum.manage")
    rule = db.get(RequirementRule, rule_id)
    if rule is None or rule.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Requirement rule not found.")
    before = rule.as_dict()
    editable = {
        "label", "kind", "operator", "target_value", "parameters", "scope", "severity",
        "weight", "score_domain", "guidance", "source_reference", "is_active", "code",
    }
    for key, value in payload.items():
        if key in editable:
            setattr(rule, key, value)
    if rule.kind not in MEASURERS:
        raise HTTPException(status_code=422, detail=f"'{rule.kind}' is not a measurable kind.")
    db.add(rule)
    audit.record(db, action=AuditAction.CONFIG_CHANGE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="requirement_rule", entity_id=rule.id,
                 summary=f"Updated requirement '{rule.label}'",
                 changes=audit.diff(before, rule.as_dict()), **meta)
    db.commit()
    db.refresh(rule)
    return RequirementRuleOut.model_validate(rule)


@router.delete("/requirements/{rule_id}", summary="Remove a requirement rule")
def delete_requirement(rule_id: str, db: DbSession, principal: CurrentPrincipal,
                       tenant_id: TenantId, meta: ClientMeta):
    principal.require("curriculum.manage")
    rule = db.get(RequirementRule, rule_id)
    if rule is None or rule.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Requirement rule not found.")
    label = rule.label
    db.delete(rule)
    audit.record(db, action=AuditAction.DELETE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="requirement_rule", entity_id=rule_id,
                 summary=f"Removed requirement '{label}'", **meta)
    db.commit()
    return {"detail": f"Requirement '{label}' removed."}


# --------------------------------------------------------------------------
# Procedure catalogue
# --------------------------------------------------------------------------
@router.get("/procedures", summary="Procedure catalogue")
def list_procedures(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    specialty_id: str | None = None,
    grade: str | None = None,
    search: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
):
    stmt = select(ProcedureCatalogueItem).where(
        ProcedureCatalogueItem.tenant_id == tenant_id,
        ProcedureCatalogueItem.is_active.is_(True),
    )
    if specialty_id:
        stmt = stmt.where(ProcedureCatalogueItem.specialty_id == specialty_id)
    if grade:
        stmt = stmt.where(ProcedureCatalogueItem.grade == grade)
    if search:
        stmt = stmt.where(ProcedureCatalogueItem.name.ilike(f"%{search}%"))
    rows = db.execute(stmt.order_by(ProcedureCatalogueItem.name).limit(limit)).scalars().all()
    return [
        {"id": p.id, "code": p.code, "name": p.name, "category": p.category,
         "grade": p.grade, "specialty_id": p.specialty_id}
        for p in rows
    ]


@router.post("/procedures", status_code=status.HTTP_201_CREATED, summary="Add a procedure")
def create_procedure(payload: dict, db: DbSession, principal: CurrentPrincipal,
                     tenant_id: TenantId, meta: ClientMeta):
    principal.require("logbook.catalogue.manage")
    required = {"code", "name"}
    if missing := required - payload.keys():
        raise HTTPException(status_code=422, detail=f"Missing field(s): {', '.join(sorted(missing))}.")
    item = ProcedureCatalogueItem(
        tenant_id=tenant_id,
        code=payload["code"],
        name=payload["name"],
        category=payload.get("category", "general"),
        grade=payload.get("grade", "minor"),
        specialty_id=payload.get("specialty_id"),
        external_codes=payload.get("external_codes", {}),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "code": item.code, "name": item.name, "grade": item.grade}
