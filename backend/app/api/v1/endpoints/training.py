"""Enrolments, the rotation engine and leave."""

from __future__ import annotations

from datetime import date

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, TenantId
from app.db.base import utcnow
from app.models.curriculum import CurriculumVersion, Programme
from app.models.enums import (
    ApprovalStatus,
    AuditAction,
    CurriculumStatus,
    EnrolmentStatus,
    RotationStatus,
)
from app.models.identity import User
from app.models.tenancy import OrgUnit
from app.models.training import Enrolment, LeaveRecord, RotationAssignment
from app.schemas.common import Page
from app.schemas.training import (
    EnrolmentCreate,
    EnrolmentOut,
    LeaveCreate,
    LeaveOut,
    RotationAssignmentOut,
    RotationCloseRequest,
    RotationExtendRequest,
    RotationPlanOut,
)
from app.services import audit, rotation as rotation_engine

router = APIRouter()


# --------------------------------------------------------------------------
def _enrolment_out(db, e: Enrolment) -> EnrolmentOut:
    out = EnrolmentOut.model_validate(e)
    out.trainee_name = e.trainee.full_name if e.trainee else None
    out.programme_name = e.programme.name if e.programme else None
    out.primary_supervisor_name = (
        e.primary_supervisor.full_name if e.primary_supervisor else None
    )
    unit = db.get(OrgUnit, e.org_unit_id)
    out.org_unit_name = unit.name if unit else None
    return out


def _rotation_out(db, r: RotationAssignment) -> RotationAssignmentOut:
    out = RotationAssignmentOut.model_validate(r)
    out.supervisor_name = r.supervisor.full_name if r.supervisor else None
    unit = db.get(OrgUnit, r.org_unit_id)
    out.org_unit_name = unit.name if unit else None
    return out


def _load_enrolment(db, tenant_id: str, enrolment_id: str) -> Enrolment:
    enrolment = db.get(Enrolment, enrolment_id)
    if enrolment is None or enrolment.tenant_id != tenant_id or enrolment.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Enrolment not found.")
    return enrolment


def _authorise_enrolment_read(principal, enrolment: Enrolment) -> None:
    if principal.is_superuser or enrolment.trainee_id == principal.id:
        return
    if principal.has("training.enrolment.read", org_unit_id=enrolment.org_unit_id):
        return
    if enrolment.primary_supervisor_id == principal.id:
        return
    raise HTTPException(status_code=403, detail="You cannot view this enrolment.")


# --------------------------------------------------------------------------
# Enrolments
# --------------------------------------------------------------------------
@router.post("/enrolments", response_model=EnrolmentOut, status_code=status.HTTP_201_CREATED,
             summary="Enrol a trainee on a programme")
def create_enrolment(
    payload: EnrolmentCreate,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    principal.require("training.enrolment.manage")

    trainee = db.get(User, payload.trainee_id)
    if trainee is None or trainee.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Trainee not found in this institution.")

    programme = db.get(Programme, payload.programme_id)
    if programme is None or programme.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Programme not found.")

    version_id = payload.curriculum_version_id
    if version_id is None:
        active = db.execute(
            select(CurriculumVersion).where(
                CurriculumVersion.programme_id == programme.id,
                CurriculumVersion.status == CurriculumStatus.ACTIVE,
            ).order_by(CurriculumVersion.effective_from.desc())
        ).scalars().first()
        if active is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Programme '{programme.name}' has no active curriculum version. "
                       "Publish one before enrolling trainees.",
            )
        version_id = active.id

    start = payload.start_date
    enrolment = Enrolment(
        tenant_id=tenant_id,
        trainee_id=trainee.id,
        programme_id=programme.id,
        curriculum_version_id=version_id,
        org_unit_id=payload.org_unit_id or programme.org_unit_id,
        primary_supervisor_id=payload.primary_supervisor_id,
        cohort_year=payload.cohort_year or start.year,
        current_level=payload.current_level or programme.entry_level,
        current_year=1,
        status=EnrolmentStatus.ACTIVE,
        start_date=start,
        expected_end_date=start + relativedelta(months=programme.duration_months),
    )
    db.add(enrolment)
    db.flush()

    if payload.generate_rotations:
        try:
            planned = rotation_engine.plan_schedule(db, enrolment)
            rotation_engine.materialise(db, enrolment, planned)
        except rotation_engine.RotationPlanningError as exc:
            # Enrol regardless — the schedule can be generated once the curriculum has
            # rotation templates. Losing the enrolment over this would be unhelpful.
            audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                         entity_type="enrolment", entity_id=enrolment.id,
                         summary=f"Enrolled without a rotation schedule: {exc}",
                         succeeded=False, **meta)

    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="enrolment", entity_id=enrolment.id,
                 summary=f"Enrolled {trainee.full_name} on {programme.name}", **meta)
    db.commit()
    db.refresh(enrolment)
    return _enrolment_out(db, enrolment)


@router.get("/enrolments", response_model=Page[EnrolmentOut], summary="List enrolments")
def list_enrolments(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    org_unit_id: str | None = None,
    programme_id: str | None = None,
    training_year: int | None = None,
    enrolment_status: str | None = Query(default=None, alias="status"),
    rag: str | None = None,
    supervisor_id: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    stmt = select(Enrolment).where(
        Enrolment.tenant_id == tenant_id, Enrolment.deleted_at.is_(None)
    )

    if not principal.is_superuser and not principal.has("training.enrolment.read"):
        stmt = stmt.where(
            (Enrolment.trainee_id == principal.id)
            | (Enrolment.primary_supervisor_id == principal.id)
        )
    elif not principal.is_superuser and principal.visible_org_unit_ids:
        stmt = stmt.where(Enrolment.org_unit_id.in_(principal.visible_org_unit_ids))

    if org_unit_id:
        stmt = stmt.where(Enrolment.org_unit_id == org_unit_id)
    if programme_id:
        stmt = stmt.where(Enrolment.programme_id == programme_id)
    if training_year:
        stmt = stmt.where(Enrolment.current_year == training_year)
    if enrolment_status:
        stmt = stmt.where(Enrolment.status == enrolment_status)
    if rag:
        stmt = stmt.where(Enrolment.latest_rag == rag)
    if supervisor_id:
        stmt = stmt.where(Enrolment.primary_supervisor_id == supervisor_id)
    if search:
        like = f"%{search}%"
        stmt = stmt.join(User, User.id == Enrolment.trainee_id).where(
            User.first_name.ilike(like) | User.last_name.ilike(like) | User.email.ilike(like)
        )

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(Enrolment.current_year.desc(), Enrolment.start_date.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()
    return Page[EnrolmentOut](
        items=[_enrolment_out(db, e) for e in rows], total=total, page=page, page_size=page_size
    )


@router.get("/enrolments/{enrolment_id}", response_model=EnrolmentOut, summary="Read an enrolment")
def get_enrolment(enrolment_id: str, db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    enrolment = _load_enrolment(db, tenant_id, enrolment_id)
    _authorise_enrolment_read(principal, enrolment)
    return _enrolment_out(db, enrolment)


# --------------------------------------------------------------------------
# Rotation engine
# --------------------------------------------------------------------------
@router.post("/enrolments/{enrolment_id}/rotations/plan", response_model=RotationPlanOut,
             summary="Preview an automatically generated rotation schedule")
def plan_rotations(
    enrolment_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    from_year: int = 1,
    to_year: int | None = None,
):
    """Dry run: shows the schedule the engine would create, with capacity warnings, so a
    coordinator can review before committing."""
    principal.require("training.rotation.manage")
    enrolment = _load_enrolment(db, tenant_id, enrolment_id)
    try:
        planned = rotation_engine.plan_schedule(db, enrolment, from_year=from_year, to_year=to_year)
    except rotation_engine.RotationPlanningError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return RotationPlanOut(
        enrolment_id=enrolment.id,
        planned=[
            {
                "template_id": p.template_id,
                "name": p.name,
                "org_unit_id": p.org_unit_id,
                "training_year": p.training_year,
                "sequence": p.sequence,
                "start_date": str(p.start_date),
                "end_date": str(p.end_date),
                "is_elective": p.is_elective,
                "objectives": p.objectives,
                "supervisor_id": p.supervisor_id,
                "supervisor_rationale": p.supervisor_rationale,
            }
            for p in planned
        ],
        capacity_warnings=rotation_engine.capacity_report(db, planned, tenant_id=tenant_id),
    )


@router.post("/enrolments/{enrolment_id}/rotations/generate",
             response_model=list[RotationAssignmentOut], summary="Generate the rotation schedule")
def generate_rotations(
    enrolment_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    from_year: int = 1,
    to_year: int | None = None,
    replace_planned: bool = True,
):
    principal.require("training.rotation.manage")
    enrolment = _load_enrolment(db, tenant_id, enrolment_id)
    try:
        planned = rotation_engine.plan_schedule(db, enrolment, from_year=from_year, to_year=to_year)
    except rotation_engine.RotationPlanningError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    created = rotation_engine.materialise(db, enrolment, planned, replace=replace_planned)
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="rotation_assignment", entity_id=enrolment.id,
                 summary=f"Generated {len(created)} rotations", **meta)
    db.commit()
    return [_rotation_out(db, r) for r in created]


@router.get("/rotations", response_model=Page[RotationAssignmentOut], summary="List rotations")
def list_rotations(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    enrolment_id: str | None = None,
    supervisor_id: str | None = None,
    org_unit_id: str | None = None,
    rotation_status: str | None = Query(default=None, alias="status"),
    active_on: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
    stmt = select(RotationAssignment).where(RotationAssignment.tenant_id == tenant_id)
    if not principal.is_superuser and not principal.has("training.rotation.read"):
        own = select(Enrolment.id).where(Enrolment.trainee_id == principal.id)
        stmt = stmt.where(
            RotationAssignment.enrolment_id.in_(own)
            | (RotationAssignment.supervisor_id == principal.id)
        )
    if enrolment_id:
        stmt = stmt.where(RotationAssignment.enrolment_id == enrolment_id)
    if supervisor_id:
        stmt = stmt.where(RotationAssignment.supervisor_id == supervisor_id)
    if org_unit_id:
        stmt = stmt.where(RotationAssignment.org_unit_id == org_unit_id)
    if rotation_status:
        stmt = stmt.where(RotationAssignment.status == rotation_status)
    if active_on:
        stmt = stmt.where(
            RotationAssignment.start_date <= active_on, RotationAssignment.end_date >= active_on
        )

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(RotationAssignment.start_date.asc())
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return Page[RotationAssignmentOut](
        items=[_rotation_out(db, r) for r in rows], total=total, page=page, page_size=page_size
    )


@router.get("/rotations/{rotation_id}/completion", summary="Rotation completion checklist")
def rotation_completion(rotation_id: str, db: DbSession, principal: CurrentPrincipal,
                        tenant_id: TenantId):
    rot = db.get(RotationAssignment, rotation_id)
    if rot is None or rot.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Rotation not found.")
    _authorise_enrolment_read(principal, rot.enrolment)
    return rotation_engine.evaluate_completion(db, rot)


@router.post("/rotations/{rotation_id}/close", response_model=RotationAssignmentOut,
             summary="Sign off a rotation")
def close_rotation(
    rotation_id: str,
    payload: RotationCloseRequest,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    principal.require("training.rotation.manage")
    rot = db.get(RotationAssignment, rotation_id)
    if rot is None or rot.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Rotation not found.")
    if payload.force and not payload.comment:
        raise HTTPException(
            status_code=422,
            detail="Closing a rotation with outstanding requirements requires a supervisor comment.",
        )
    try:
        rotation_engine.close_rotation(
            db, rot, closed_by_id=principal.id, outcome=payload.outcome,
            comment=payload.comment, force=payload.force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    audit.record(db, action=AuditAction.APPROVE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="rotation_assignment", entity_id=rot.id,
                 summary=f"Rotation closed as {payload.outcome}"
                         + (" (forced)" if payload.force else ""), **meta)
    db.commit()
    db.refresh(rot)
    return _rotation_out(db, rot)


@router.post("/rotations/{rotation_id}/extend", response_model=list[RotationAssignmentOut],
             summary="Extend a rotation")
def extend_rotation(
    rotation_id: str,
    payload: RotationExtendRequest,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    principal.require("training.rotation.manage")
    rot = db.get(RotationAssignment, rotation_id)
    if rot is None or rot.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Rotation not found.")
    try:
        touched = rotation_engine.extend_rotation(
            db, rot, new_end_date=payload.new_end_date, reason=payload.reason,
            cascade=payload.cascade,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.record(db, action=AuditAction.UPDATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="rotation_assignment", entity_id=rot.id,
                 summary=f"Extended to {payload.new_end_date}; {len(touched) - 1} downstream "
                         f"rotation(s) shifted", **meta)
    db.commit()
    return [_rotation_out(db, r) for r in touched]


@router.post("/rotations/{rotation_id}/remedial", response_model=RotationAssignmentOut,
             summary="Create a remedial posting")
def create_remedial(
    rotation_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    weeks: int = Query(ge=1, le=52),
    reason: str | None = None,
    start_date: date | None = None,
):
    principal.require("training.rotation.manage")
    rot = db.get(RotationAssignment, rotation_id)
    if rot is None or rot.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Rotation not found.")
    remedial = rotation_engine.create_remedial(
        db, rot, weeks=weeks, start_date=start_date, reason=reason
    )
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="rotation_assignment", entity_id=rot.id,
                 summary=f"Created {weeks}-week remedial posting", **meta)
    db.commit()
    db.refresh(remedial)
    return _rotation_out(db, remedial)


# --------------------------------------------------------------------------
# Leave
# --------------------------------------------------------------------------
@router.post("/enrolments/{enrolment_id}/leave", response_model=LeaveOut,
             status_code=status.HTTP_201_CREATED, summary="Request leave")
def request_leave(
    enrolment_id: str,
    payload: LeaveCreate,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    enrolment = _load_enrolment(db, tenant_id, enrolment_id)
    if enrolment.trainee_id != principal.id:
        principal.require("training.leave.approve", org_unit_id=enrolment.org_unit_id)
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=422, detail="The end date cannot precede the start date.")

    leave = LeaveRecord(
        tenant_id=tenant_id,
        enrolment_id=enrolment.id,
        leave_type=payload.leave_type,
        start_date=payload.start_date,
        end_date=payload.end_date,
        extends_training=payload.extends_training,
        reason=payload.reason,
        status=ApprovalStatus.SUBMITTED,
    )
    db.add(leave)
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="leave_record", entity_id=leave.id,
                 summary=f"{payload.leave_type} leave requested "
                         f"({payload.start_date}–{payload.end_date})", **meta)
    db.commit()
    db.refresh(leave)
    return LeaveOut.model_validate(leave)


@router.post("/leave/{leave_id}/decision", response_model=LeaveOut, summary="Approve or decline leave")
def decide_leave(
    leave_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    decision: str = Query(description="approved | rejected"),
    note: str | None = None,
):
    principal.require("training.leave.approve")
    leave = db.get(LeaveRecord, leave_id)
    if leave is None or leave.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Leave request not found.")
    if decision not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
        raise HTTPException(status_code=422, detail="Decision must be 'approved' or 'rejected'.")

    leave.status = decision
    leave.approver_id = principal.id
    leave.decided_at = utcnow()
    leave.decision_note = note
    db.add(leave)

    shifted: list = []
    if decision == ApprovalStatus.APPROVED:
        shifted = rotation_engine.apply_leave_interruption(db, leave)
        if leave.extends_training:
            enrolment = leave.enrolment
            if enrolment.status == EnrolmentStatus.ACTIVE and leave.days >= 14:
                enrolment.status = EnrolmentStatus.ON_LEAVE
                db.add(enrolment)

    audit.record(db, action=AuditAction.APPROVE if decision == ApprovalStatus.APPROVED
                 else AuditAction.REJECT,
                 tenant_id=tenant_id, actor_id=principal.id, entity_type="leave_record",
                 entity_id=leave.id,
                 summary=f"Leave {decision}; {len(shifted)} rotation(s) rescheduled", **meta)
    db.commit()
    db.refresh(leave)
    return LeaveOut.model_validate(leave)


@router.get("/leave", response_model=list[LeaveOut], summary="List leave records")
def list_leave(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    enrolment_id: str | None = None,
    leave_status: str | None = Query(default=None, alias="status"),
):
    stmt = select(LeaveRecord).where(LeaveRecord.tenant_id == tenant_id)
    if enrolment_id:
        stmt = stmt.where(LeaveRecord.enrolment_id == enrolment_id)
    if leave_status:
        stmt = stmt.where(LeaveRecord.status == leave_status)
    if not principal.is_superuser and not principal.has("training.leave.approve"):
        own = select(Enrolment.id).where(Enrolment.trainee_id == principal.id)
        stmt = stmt.where(LeaveRecord.enrolment_id.in_(own))
    rows = db.execute(stmt.order_by(LeaveRecord.start_date.desc())).scalars().all()
    return [LeaveOut.model_validate(r) for r in rows]


@router.post("/maintenance/refresh-rotation-statuses", summary="Advance rotation statuses")
def refresh_statuses(db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    """Idempotent daily maintenance: activates rotations that have started and reports
    those past their end date awaiting sign-off."""
    principal.require("training.rotation.manage")
    result = rotation_engine.refresh_statuses(db, tenant_id)
    db.commit()
    return result
