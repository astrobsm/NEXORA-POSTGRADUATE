"""Research projects, the dissertation workflow and supervisor allocation."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, TenantId
from app.db.base import utcnow
from app.models.enums import (
    DISSERTATION_STAGE_ORDER,
    ApprovalStatus,
    AuditAction,
    DissertationStage,
)
from app.models.research import (
    DissertationMilestone,
    ProjectSupervision,
    Publication,
    ResearchProject,
    SupervisionMeeting,
)
from app.models.training import Enrolment
from app.schemas.common import CandidateOut, Page
from app.services import allocation, audit

router = APIRouter()

#: The default dissertation workflow. Institutions may shorten or extend it by editing
#: ``Tenant.settings["dissertation_stages"]``.
DEFAULT_MILESTONES: list[tuple[str, str, int]] = [
    (DissertationStage.SUPERVISOR_ASSIGNMENT, "Supervisor assignment", 0),
    (DissertationStage.TOPIC_APPROVAL, "Topic approval", 30),
    (DissertationStage.PROPOSAL_WRITING, "Proposal writing", 90),
    (DissertationStage.PROPOSAL_DEFENCE, "Proposal defence", 150),
    (DissertationStage.ETHICS_APPROVAL, "Ethics approval", 180),
    (DissertationStage.DATA_COLLECTION, "Data collection", 450),
    (DissertationStage.ANALYSIS, "Data analysis", 540),
    (DissertationStage.DRAFT_SUBMISSION, "Draft submission", 600),
    (DissertationStage.CORRECTIONS, "Corrections", 660),
    (DissertationStage.FINAL_DEFENCE, "Final defence", 720),
    (DissertationStage.COLLEGE_SUBMISSION, "College submission", 750),
]


def _milestone_plan(db, tenant_id: str) -> list[tuple[str, str, int]]:
    """The milestone plan for this institution.

    Falls back to the default workflow unless the tenant defines its own under
    ``settings["dissertation_stages"]`` as a list of
    ``{"stage": ..., "title": ..., "offset_days": ...}`` objects.
    """
    from app.models.tenancy import Tenant

    tenant = db.get(Tenant, tenant_id)
    custom = (tenant.settings or {}).get("dissertation_stages") if tenant else None
    if not isinstance(custom, list) or not custom:
        return DEFAULT_MILESTONES

    plan: list[tuple[str, str, int]] = []
    for item in custom:
        stage = item.get("stage")
        if stage not in DISSERTATION_STAGE_ORDER:
            continue
        plan.append((stage, item.get("title", stage.replace("_", " ").title()),
                     int(item.get("offset_days", 0))))
    return plan or DEFAULT_MILESTONES


def _project_out(db, p: ResearchProject) -> dict:
    supervisors = [
        {
            "user_id": s.supervisor_id,
            "name": s.supervisor.full_name if s.supervisor else None,
            "is_primary": s.is_primary,
            "assigned_on": s.assigned_on,
            "allocation_method": s.allocation_method,
        }
        for s in p.supervisions
        if s.ended_on is None
    ]
    completed = sum(1 for m in p.milestones if m.status == ApprovalStatus.APPROVED)
    return {
        "id": p.id,
        "title": p.title,
        "research_type": p.research_type,
        "submitting_body": p.submitting_body,
        "enrolment_id": p.enrolment_id,
        "principal_investigator_id": p.principal_investigator_id,
        "principal_investigator": p.principal_investigator.full_name if p.principal_investigator else None,
        "current_stage": p.current_stage,
        "stage_index": (
            DISSERTATION_STAGE_ORDER.index(p.current_stage)
            if p.current_stage in DISSERTATION_STAGE_ORDER else None
        ),
        "total_stages": len(DISSERTATION_STAGE_ORDER),
        "status": p.status,
        "progress_percent": p.progress_percent,
        "ethics_status": p.ethics_status,
        "ethics_reference": p.ethics_reference,
        "started_on": p.started_on,
        "target_completion_on": p.target_completion_on,
        "completed_on": p.completed_on,
        "keywords": p.keywords,
        "supervisors": supervisors,
        "milestones_total": len(p.milestones),
        "milestones_completed": completed,
    }


# --------------------------------------------------------------------------
@router.post("/projects", status_code=status.HTTP_201_CREATED, summary="Register a project")
def create_project(
    payload: dict,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    auto_assign_supervisor: bool = True,
    create_milestones: bool = True,
):
    """Register a dissertation or research project.

    When ``auto_assign_supervisor`` is set the allocator picks the best-fit supervisor
    using expertise, availability, workload, history and conflicts of interest — and
    stores the reasoning alongside the assignment.
    """
    principal.require("research.project.create")
    if not payload.get("title"):
        raise HTTPException(status_code=422, detail="A project title is required.")

    enrolment_id = payload.get("enrolment_id")
    enrolment = db.get(Enrolment, enrolment_id) if enrolment_id else None
    if enrolment_id and (enrolment is None or enrolment.tenant_id != tenant_id):
        raise HTTPException(status_code=404, detail="Enrolment not found.")

    org_unit_id = payload.get("org_unit_id") or (enrolment.org_unit_id if enrolment else None)
    if not org_unit_id:
        raise HTTPException(status_code=422, detail="An organisational unit is required.")

    project = ResearchProject(
        tenant_id=tenant_id,
        org_unit_id=org_unit_id,
        enrolment_id=enrolment_id,
        principal_investigator_id=payload.get("principal_investigator_id") or principal.id,
        title=payload["title"],
        research_type=payload.get("research_type", "dissertation"),
        submitting_body=payload.get("submitting_body"),
        background=payload.get("background"),
        aim=payload.get("aim"),
        objectives=payload.get("objectives", []),
        methodology=payload.get("methodology"),
        study_design=payload.get("study_design"),
        setting=payload.get("setting"),
        sample_size=payload.get("sample_size"),
        keywords=payload.get("keywords", []),
        current_stage=DissertationStage.CONCEPT,
        status=ApprovalStatus.SUBMITTED,
        started_on=date.today(),
        target_completion_on=payload.get("target_completion_on"),
        ethics_required=payload.get("ethics_required", True),
    )
    db.add(project)
    db.flush()

    allocation_note = None
    if auto_assign_supervisor:
        try:
            supervision = allocation.assign_research_supervisor(db, project)
            project.current_stage = DissertationStage.SUPERVISOR_ASSIGNMENT
            allocation_note = supervision.allocation_score.get("chosen", {}).get("name")
        except ValueError as exc:
            allocation_note = f"Not assigned: {exc}"

    if create_milestones:
        base = date.today()
        for index, (stage, title, offset_days) in enumerate(_milestone_plan(db, tenant_id)):
            db.add(
                DissertationMilestone(
                    tenant_id=tenant_id,
                    project_id=project.id,
                    stage=stage,
                    sequence=index,
                    title=title,
                    due_on=base + timedelta(days=offset_days),
                    status=ApprovalStatus.DRAFT,
                )
            )

    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="research_project", entity_id=project.id,
                 summary=f"Registered project '{project.title}'", **meta)
    db.commit()
    db.refresh(project)
    result = _project_out(db, project)
    result["allocation_note"] = allocation_note
    return result


@router.get("/projects", summary="List research projects")
def list_projects(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    enrolment_id: str | None = None,
    supervisor_id: str | None = None,
    stage: str | None = None,
    org_unit_id: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    stmt = select(ResearchProject).where(
        ResearchProject.tenant_id == tenant_id, ResearchProject.deleted_at.is_(None)
    )
    if not principal.is_superuser and not principal.has("research.project.read.any"):
        supervised = select(ProjectSupervision.project_id).where(
            ProjectSupervision.supervisor_id == principal.id
        )
        stmt = stmt.where(
            (ResearchProject.principal_investigator_id == principal.id)
            | ResearchProject.id.in_(supervised)
        )
    if enrolment_id:
        stmt = stmt.where(ResearchProject.enrolment_id == enrolment_id)
    if stage:
        stmt = stmt.where(ResearchProject.current_stage == stage)
    if org_unit_id:
        stmt = stmt.where(ResearchProject.org_unit_id == org_unit_id)
    if supervisor_id:
        stmt = stmt.where(
            ResearchProject.id.in_(
                select(ProjectSupervision.project_id).where(
                    ProjectSupervision.supervisor_id == supervisor_id,
                    ProjectSupervision.ended_on.is_(None),
                )
            )
        )

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(ResearchProject.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return {
        "items": [_project_out(db, p) for p in rows],
        "total": total, "page": page, "page_size": page_size,
    }


@router.get("/projects/{project_id}", summary="Read a project")
def get_project(project_id: str, db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    project = db.get(ResearchProject, project_id)
    if project is None or project.tenant_id != tenant_id or project.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Project not found.")
    data = _project_out(db, project)
    data["milestones"] = [
        {
            "id": m.id, "stage": m.stage, "sequence": m.sequence, "title": m.title,
            "due_on": m.due_on, "submitted_on": m.submitted_on, "completed_on": m.completed_on,
            "status": m.status, "decision_note": m.decision_note, "panel": m.panel,
        }
        for m in sorted(project.milestones, key=lambda m: m.sequence)
    ]
    return data


@router.get("/supervisor-candidates", response_model=list[CandidateOut],
            summary="Rank supervisors for a project")
def supervisor_candidates(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    org_unit_id: str,
    trainee_id: str,
    keywords: list[str] = Query(default=[]),
    include_ineligible: bool = False,
):
    """The allocator's ranked list with full reasoning, so a coordinator can see why a
    supervisor was proposed — or why someone was excluded."""
    principal.require("research.supervise")
    candidates = allocation.rank_research_supervisors(
        db, tenant_id=tenant_id, org_unit_id=org_unit_id, trainee_id=trainee_id,
        keywords=keywords, include_ineligible=include_ineligible,
    )
    return [CandidateOut(**c.as_dict()) for c in candidates]


@router.post("/projects/{project_id}/supervisors", summary="Attach a supervisor")
def add_supervisor(
    project_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    supervisor_id: str | None = None,
    is_primary: bool = True,
):
    principal.require("research.supervise")
    project = db.get(ResearchProject, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        supervision = allocation.assign_research_supervisor(
            db, project, supervisor_id=supervisor_id, is_primary=is_primary,
            allocation_method="manual" if supervisor_id else "automatic",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="project_supervision", entity_id=supervision.id,
                 summary=f"Supervisor attached to '{project.title}'", **meta)
    db.commit()
    db.refresh(supervision)
    return {
        "id": supervision.id,
        "supervisor_id": supervision.supervisor_id,
        "is_primary": supervision.is_primary,
        "allocation_method": supervision.allocation_method,
        "rationale": supervision.allocation_score,
    }


@router.post("/milestones/{milestone_id}/decision", summary="Approve or return a milestone")
def decide_milestone(
    milestone_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    decision: str = Query(description="approved | returned | rejected"),
    note: str | None = None,
):
    """Approving a milestone advances the project to that stage and recomputes progress."""
    principal.require("research.milestone.approve")
    milestone = db.get(DissertationMilestone, milestone_id)
    if milestone is None or milestone.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Milestone not found.")
    if decision not in {ApprovalStatus.APPROVED, ApprovalStatus.RETURNED, ApprovalStatus.REJECTED}:
        raise HTTPException(status_code=422, detail="Decision must be approved, returned or rejected.")
    if decision != ApprovalStatus.APPROVED and not note:
        raise HTTPException(status_code=422,
                            detail="A note is required when returning or rejecting a milestone.")

    milestone.status = decision
    milestone.approver_id = principal.id
    milestone.decision_note = note
    project = milestone.project

    if decision == ApprovalStatus.APPROVED:
        milestone.completed_on = date.today()
        if milestone.stage in DISSERTATION_STAGE_ORDER:
            current_index = (
                DISSERTATION_STAGE_ORDER.index(project.current_stage)
                if project.current_stage in DISSERTATION_STAGE_ORDER else -1
            )
            new_index = DISSERTATION_STAGE_ORDER.index(milestone.stage)
            if new_index > current_index:
                project.current_stage = milestone.stage
        if milestone.stage == DissertationStage.ETHICS_APPROVAL:
            project.ethics_status = "approved"
            project.ethics_approved_on = date.today()

    approved = sum(1 for m in project.milestones if m.status == ApprovalStatus.APPROVED)
    project.progress_percent = round(approved / max(1, len(project.milestones)) * 100, 1)
    if project.current_stage == DissertationStage.COLLEGE_SUBMISSION and approved == len(project.milestones):
        project.current_stage = DissertationStage.COMPLETED
        project.completed_on = date.today()

    db.add(milestone)
    db.add(project)
    audit.record(db, action=AuditAction.APPROVE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="dissertation_milestone", entity_id=milestone.id,
                 summary=f"Milestone '{milestone.title}' {decision}", **meta)
    db.commit()
    return {
        "milestone_id": milestone.id,
        "status": milestone.status,
        "project_stage": project.current_stage,
        "progress_percent": project.progress_percent,
    }


@router.post("/projects/{project_id}/meetings", status_code=status.HTTP_201_CREATED,
             summary="Record a supervision meeting")
def record_meeting(project_id: str, payload: dict, db: DbSession, principal: CurrentPrincipal,
                   tenant_id: TenantId):
    project = db.get(ResearchProject, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Project not found.")
    meeting = SupervisionMeeting(
        tenant_id=tenant_id,
        project_id=project_id,
        enrolment_id=project.enrolment_id,
        supervisor_id=payload.get("supervisor_id") or principal.id,
        held_on=payload.get("held_on") or date.today(),
        duration_minutes=payload.get("duration_minutes", 30),
        agenda=payload.get("agenda"),
        discussion=payload.get("discussion"),
        agreed_actions=payload.get("agreed_actions", []),
        next_meeting_on=payload.get("next_meeting_on"),
        concerns_raised=payload.get("concerns_raised", False),
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    return {"id": meeting.id, "held_on": meeting.held_on, "next_meeting_on": meeting.next_meeting_on}


# --------------------------------------------------------------------------
# Publications
# --------------------------------------------------------------------------
@router.post("/publications", status_code=status.HTTP_201_CREATED, summary="Record a publication")
def create_publication(payload: dict, db: DbSession, principal: CurrentPrincipal,
                       tenant_id: TenantId, meta: ClientMeta):
    required = {"title", "authors", "year"}
    if missing := required - payload.keys():
        raise HTTPException(status_code=422, detail=f"Missing field(s): {', '.join(sorted(missing))}.")

    enrolment_id = payload.get("enrolment_id")
    if enrolment_id is None:
        enrolment = db.execute(
            select(Enrolment).where(
                Enrolment.trainee_id == principal.id, Enrolment.deleted_at.is_(None)
            ).order_by(Enrolment.start_date.desc())
        ).scalars().first()
        enrolment_id = enrolment.id if enrolment else None

    publication = Publication(
        tenant_id=tenant_id,
        user_id=payload.get("user_id") or principal.id,
        enrolment_id=enrolment_id,
        project_id=payload.get("project_id"),
        publication_type=payload.get("publication_type", "journal_article"),
        title=payload["title"],
        authors=payload["authors"],
        author_position=payload.get("author_position", 1),
        is_corresponding=payload.get("is_corresponding", False),
        venue=payload.get("venue"),
        year=int(payload["year"]),
        volume=payload.get("volume"),
        pages=payload.get("pages"),
        doi=payload.get("doi"),
        url=payload.get("url"),
        indexed_in=payload.get("indexed_in", []),
        impact_factor=payload.get("impact_factor"),
        is_peer_reviewed=payload.get("is_peer_reviewed", True),
        status=payload.get("status", "published"),
        verification_status=ApprovalStatus.SUBMITTED,
        evidence_keys=payload.get("evidence_keys", []),
    )
    db.add(publication)
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="publication", entity_id=publication.id,
                 summary=f"Recorded publication '{publication.title}'", **meta)
    db.commit()
    db.refresh(publication)
    return {"id": publication.id, "title": publication.title,
            "verification_status": publication.verification_status}


@router.post("/publications/{publication_id}/verify", summary="Verify a publication")
def verify_publication(publication_id: str, db: DbSession, principal: CurrentPrincipal,
                       tenant_id: TenantId, meta: ClientMeta,
                       decision: str = Query(description="approved | rejected")):
    """Publications only count toward the Research Score once verified — otherwise the
    metric would be self-reported."""
    principal.require("research.project.read.any")
    publication = db.get(Publication, publication_id)
    if publication is None or publication.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Publication not found.")
    if decision not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
        raise HTTPException(status_code=422, detail="Decision must be 'approved' or 'rejected'.")
    publication.verification_status = decision
    publication.verified_by_id = principal.id
    db.add(publication)
    audit.record(db, action=AuditAction.APPROVE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="publication", entity_id=publication.id,
                 summary=f"Publication {decision}", **meta)
    db.commit()
    return {"id": publication.id, "verification_status": publication.verification_status}


@router.get("/publications", summary="List publications")
def list_publications(db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId,
                      user_id: str | None = None, enrolment_id: str | None = None,
                      year: int | None = None):
    stmt = select(Publication).where(
        Publication.tenant_id == tenant_id, Publication.deleted_at.is_(None)
    )
    if user_id:
        stmt = stmt.where(Publication.user_id == user_id)
    elif not principal.has("research.project.read.any"):
        stmt = stmt.where(Publication.user_id == principal.id)
    if enrolment_id:
        stmt = stmt.where(Publication.enrolment_id == enrolment_id)
    if year:
        stmt = stmt.where(Publication.year == year)
    rows = db.execute(stmt.order_by(Publication.year.desc())).scalars().all()
    return [
        {
            "id": p.id, "title": p.title, "authors": p.authors, "venue": p.venue,
            "year": p.year, "doi": p.doi, "publication_type": p.publication_type,
            "author_position": p.author_position, "indexed_in": p.indexed_in,
            "verification_status": p.verification_status, "is_peer_reviewed": p.is_peer_reviewed,
        }
        for p in rows
    ]
