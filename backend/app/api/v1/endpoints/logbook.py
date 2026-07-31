"""The digital logbook: capture, validation queue, consultant sign-off and summaries."""

from __future__ import annotations

from collections import Counter
from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, TenantId
from app.db.base import utcnow
from app.models.assessment import CompetencyRating
from app.models.curriculum import Competency
from app.models.enums import (
    ENTRUSTMENT_ORDER,
    AuditAction,
    LogEntryType,
    ValidationStatus,
)
from app.models.logbook import LogEntry, LogEntryAudit
from app.models.training import Enrolment
from app.schemas.common import Page
from app.schemas.training import (
    LogbookSummary,
    LogEntryCreate,
    LogEntryOut,
    LogEntryUpdate,
    LogValidationRequest,
)
from app.services import audit

router = APIRouter()

MAJOR_TYPES = {LogEntryType.MAJOR_PROCEDURE}
MINOR_TYPES = {LogEntryType.MINOR_PROCEDURE}


# --------------------------------------------------------------------------
def _own_enrolment(db, principal, enrolment_id: str | None) -> Enrolment:
    """Resolve the enrolment a trainee is writing against."""
    stmt = select(Enrolment).where(
        Enrolment.trainee_id == principal.id, Enrolment.deleted_at.is_(None)
    )
    if enrolment_id:
        stmt = stmt.where(Enrolment.id == enrolment_id)
    enrolment = db.execute(stmt.order_by(Enrolment.start_date.desc())).scalars().first()
    if enrolment is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You have no training enrolment, so logbook entries cannot be recorded.",
        )
    return enrolment


def _can_read_entry(db, principal, entry: LogEntry) -> bool:
    if principal.is_superuser:
        return True
    enrolment = db.get(Enrolment, entry.enrolment_id)
    if enrolment and enrolment.trainee_id == principal.id:
        return True
    if principal.has("logbook.entry.read.any", org_unit_id=entry.org_unit_id):
        return True
    return bool(principal.has("logbook.entry.read.supervised") and entry.supervisor_id == principal.id)


def _serialise(db, entry: LogEntry) -> LogEntryOut:
    out = LogEntryOut.model_validate(entry)
    if entry.supervisor is not None:
        out.supervisor_name = entry.supervisor.full_name
    return out


# --------------------------------------------------------------------------
@router.post("", response_model=LogEntryOut, status_code=status.HTTP_201_CREATED,
             summary="Record a logbook entry")
def create_entry(
    payload: LogEntryCreate,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    enrolment_id: str | None = Query(default=None),
):
    """Create an entry. Entries land as ``pending`` and count toward nothing until a
    consultant validates them — the platform never lets unvalidated activity inflate a
    trainee's numbers."""
    principal.require("logbook.entry.create")
    enrolment = _own_enrolment(db, principal, enrolment_id or (payload.rotation_assignment_id and None))

    # Idempotent replay of an offline-created entry.
    if payload.client_uuid:
        existing = db.execute(
            select(LogEntry).where(
                LogEntry.client_uuid == payload.client_uuid,
                LogEntry.enrolment_id == enrolment.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _serialise(db, existing)

    rotation = enrolment.current_rotation(payload.occurred_at.date())
    supervisor_id = payload.supervisor_id or (rotation.supervisor_id if rotation else None) \
        or enrolment.primary_supervisor_id

    entry = LogEntry(
        tenant_id=tenant_id,
        enrolment_id=enrolment.id,
        rotation_assignment_id=payload.rotation_assignment_id or (rotation.id if rotation else None),
        org_unit_id=payload.org_unit_id or (rotation.org_unit_id if rotation else enrolment.org_unit_id),
        entry_type=payload.entry_type,
        occurred_at=payload.occurred_at,
        occurred_on=payload.occurred_at.date(),
        title=payload.title,
        summary=payload.summary,
        patient_reference=payload.patient_reference,
        patient_age_years=payload.patient_age_years,
        patient_age_months=payload.patient_age_months,
        patient_sex=payload.patient_sex,
        setting=payload.setting,
        diagnosis=payload.diagnosis,
        diagnosis_codes=payload.diagnosis_codes,
        procedure_id=payload.procedure_id,
        procedure_name=payload.procedure_name,
        procedure_grade=payload.procedure_grade,
        participation_role=payload.participation_role,
        complexity=payload.complexity,
        outcome=payload.outcome,
        complication_detail=payload.complication_detail,
        anaesthesia_type=payload.anaesthesia_type,
        duration_minutes=payload.duration_minutes,
        quantity=max(1, payload.quantity),
        reflection=payload.reflection,
        learning_points=payload.learning_points,
        attachment_keys=payload.attachment_keys,
        supervisor_id=supervisor_id,
        validation_status=ValidationStatus.PENDING,
        captured_offline=payload.captured_offline,
        client_uuid=payload.client_uuid,
    )

    if payload.competency_ids:
        entry.competencies = list(
            db.execute(select(Competency).where(Competency.id.in_(payload.competency_ids)))
            .scalars()
            .all()
        )

    db.add(entry)
    db.flush()
    db.add(
        LogEntryAudit(
            tenant_id=tenant_id,
            log_entry_id=entry.id,
            actor_id=principal.id,
            action="created",
            to_status=ValidationStatus.PENDING,
            comment="Captured offline" if payload.captured_offline else None,
        )
    )
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="log_entry", entity_id=entry.id,
                 summary=f"Logged {entry.entry_type}: {entry.title}",
                 via_sync=payload.captured_offline, **meta)
    db.commit()
    db.refresh(entry)
    return _serialise(db, entry)


@router.get("", response_model=Page[LogEntryOut], summary="List logbook entries")
def list_entries(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    enrolment_id: str | None = None,
    trainee_id: str | None = None,
    entry_type: str | None = None,
    validation_status: str | None = None,
    rotation_assignment_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    stmt = select(LogEntry).where(
        LogEntry.tenant_id == tenant_id, LogEntry.deleted_at.is_(None)
    )

    # Scope: trainees see their own; supervisors see what they supervise; leadership
    # sees everything within the org units they hold permission at.
    if not principal.is_superuser and not principal.has("logbook.entry.read.any"):
        own = select(Enrolment.id).where(Enrolment.trainee_id == principal.id)
        if principal.has("logbook.entry.read.supervised"):
            stmt = stmt.where(
                (LogEntry.enrolment_id.in_(own)) | (LogEntry.supervisor_id == principal.id)
            )
        else:
            stmt = stmt.where(LogEntry.enrolment_id.in_(own))

    if enrolment_id:
        stmt = stmt.where(LogEntry.enrolment_id == enrolment_id)
    if trainee_id:
        stmt = stmt.where(
            LogEntry.enrolment_id.in_(
                select(Enrolment.id).where(Enrolment.trainee_id == trainee_id)
            )
        )
    if entry_type:
        stmt = stmt.where(LogEntry.entry_type == entry_type)
    if validation_status:
        stmt = stmt.where(LogEntry.validation_status == validation_status)
    if rotation_assignment_id:
        stmt = stmt.where(LogEntry.rotation_assignment_id == rotation_assignment_id)
    if date_from:
        stmt = stmt.where(LogEntry.occurred_on >= date_from)
    if date_to:
        stmt = stmt.where(LogEntry.occurred_on <= date_to)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            LogEntry.title.ilike(like)
            | LogEntry.diagnosis.ilike(like)
            | LogEntry.procedure_name.ilike(like)
        )

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(LogEntry.occurred_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()

    return Page[LogEntryOut](
        items=[_serialise(db, e) for e in rows], total=total, page=page, page_size=page_size
    )


@router.get("/pending-validation", response_model=Page[LogEntryOut],
            summary="Entries awaiting your sign-off")
def pending_validation(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    principal.require("logbook.entry.validate")
    stmt = select(LogEntry).where(
        LogEntry.tenant_id == tenant_id,
        LogEntry.deleted_at.is_(None),
        LogEntry.validation_status == ValidationStatus.PENDING,
        LogEntry.supervisor_id == principal.id,
    )
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(LogEntry.occurred_at.asc()).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return Page[LogEntryOut](
        items=[_serialise(db, e) for e in rows], total=total, page=page, page_size=page_size
    )


@router.get("/summary", response_model=LogbookSummary, summary="Logbook statistics")
def summary(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    enrolment_id: str | None = None,
):
    if enrolment_id is None:
        enrolment = _own_enrolment(db, principal, None)
        enrolment_id = enrolment.id
    else:
        enrolment = db.get(Enrolment, enrolment_id)
        if enrolment is None or enrolment.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Enrolment not found.")
        if enrolment.trainee_id != principal.id:
            principal.require("logbook.entry.read.any", org_unit_id=enrolment.org_unit_id)

    rows = db.execute(
        select(LogEntry).where(
            LogEntry.enrolment_id == enrolment_id, LogEntry.deleted_at.is_(None)
        )
    ).scalars().all()

    by_status = Counter(e.validation_status for e in rows)
    by_type = Counter(e.entry_type for e in rows)
    by_role = Counter(e.participation_role for e in rows if e.participation_role)
    by_month = Counter(e.occurred_on.strftime("%Y-%m") for e in rows)

    return LogbookSummary(
        total=len(rows),
        validated=by_status.get(ValidationStatus.VALIDATED, 0),
        pending=by_status.get(ValidationStatus.PENDING, 0),
        queried=by_status.get(ValidationStatus.QUERIED, 0),
        rejected=by_status.get(ValidationStatus.REJECTED, 0),
        by_type=dict(by_type),
        by_role=dict(by_role),
        by_month=dict(sorted(by_month.items())),
        major_procedures=sum(e.quantity for e in rows if e.entry_type in MAJOR_TYPES),
        minor_procedures=sum(e.quantity for e in rows if e.entry_type in MINOR_TYPES),
    )


@router.get("/{entry_id}", response_model=LogEntryOut, summary="Read one entry")
def get_entry(entry_id: str, db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    entry = db.get(LogEntry, entry_id)
    if entry is None or entry.tenant_id != tenant_id or entry.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Logbook entry not found.")
    if not _can_read_entry(db, principal, entry):
        raise HTTPException(status_code=403, detail="You cannot view this logbook entry.")
    return _serialise(db, entry)


@router.patch("/{entry_id}", response_model=LogEntryOut, summary="Amend an entry")
def update_entry(
    entry_id: str,
    payload: LogEntryUpdate,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    """Amend an entry. Validated entries are locked — a signed-off logbook is evidence,
    and changing it after the fact would undermine that. Queried entries may be
    corrected and resubmitted."""
    entry = db.get(LogEntry, entry_id)
    if entry is None or entry.tenant_id != tenant_id or entry.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Logbook entry not found.")

    enrolment = db.get(Enrolment, entry.enrolment_id)
    if enrolment.trainee_id != principal.id:
        raise HTTPException(status_code=403, detail="You can only amend your own entries.")
    if entry.validation_status == ValidationStatus.VALIDATED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This entry has been validated and can no longer be edited. "
                   "Ask your supervisor to return it if a correction is needed.",
        )

    before = entry.as_dict()
    data = payload.model_dump(exclude_unset=True)
    competency_ids = data.pop("competency_ids", None)
    for field, value in data.items():
        setattr(entry, field, value)
    if competency_ids is not None:
        entry.competencies = list(
            db.execute(select(Competency).where(Competency.id.in_(competency_ids))).scalars().all()
        )
    if entry.validation_status == ValidationStatus.QUERIED:
        entry.validation_status = ValidationStatus.PENDING

    db.add(entry)
    db.add(
        LogEntryAudit(
            tenant_id=tenant_id,
            log_entry_id=entry.id,
            actor_id=principal.id,
            action="amended",
            from_status=before.get("validation_status"),
            to_status=entry.validation_status,
            changes=audit.diff(before, entry.as_dict()),
        )
    )
    audit.record(db, action=AuditAction.UPDATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="log_entry", entity_id=entry.id, summary="Amended logbook entry",
                 changes=audit.diff(before, entry.as_dict()), **meta)
    db.commit()
    db.refresh(entry)
    return _serialise(db, entry)


@router.post("/{entry_id}/validation", response_model=LogEntryOut,
             summary="Validate, query or reject an entry")
def validate_entry(
    entry_id: str,
    payload: LogValidationRequest,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    """Consultant sign-off. Optionally awards entrustment ratings in the same action,
    which is how most supervisors actually work — judgement and evidence together."""
    principal.require("logbook.entry.validate")
    entry = db.get(LogEntry, entry_id)
    if entry is None or entry.tenant_id != tenant_id or entry.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Logbook entry not found.")

    valid = {ValidationStatus.VALIDATED, ValidationStatus.QUERIED, ValidationStatus.REJECTED}
    if payload.decision not in valid:
        raise HTTPException(
            status_code=422,
            detail=f"Decision must be one of {', '.join(sorted(valid))}.",
        )
    if payload.decision in {ValidationStatus.QUERIED, ValidationStatus.REJECTED} and not payload.comment:
        raise HTTPException(
            status_code=422,
            detail="A comment is required when returning or rejecting an entry, so the "
                   "trainee knows what to correct.",
        )

    before_status = entry.validation_status
    entry.validation_status = payload.decision
    entry.validator_comment = payload.comment
    entry.validated_by_id = principal.id
    entry.validated_at = utcnow()
    if payload.decision == ValidationStatus.QUERIED:
        entry.query_count += 1
    db.add(entry)

    enrolment = db.get(Enrolment, entry.enrolment_id)
    for rating in payload.competency_ratings:
        competency_id = rating.get("competency_id")
        level = rating.get("level")
        if not competency_id or not level:
            continue
        db.add(
            CompetencyRating(
                tenant_id=tenant_id,
                enrolment_id=enrolment.id,
                competency_id=competency_id,
                rotation_assignment_id=entry.rotation_assignment_id,
                assessor_id=principal.id,
                level=level,
                level_value=ENTRUSTMENT_ORDER.get(level, 2),
                rated_on=date.today(),
                evidence=f"Awarded on validation of logbook entry '{entry.title}'.",
            )
        )

    db.add(
        LogEntryAudit(
            tenant_id=tenant_id,
            log_entry_id=entry.id,
            actor_id=principal.id,
            action="validation",
            from_status=before_status,
            to_status=payload.decision,
            comment=payload.comment,
        )
    )
    audit.record(db, action=AuditAction.VALIDATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="log_entry", entity_id=entry.id,
                 summary=f"Logbook entry {payload.decision}", **meta)
    db.commit()
    db.refresh(entry)
    return _serialise(db, entry)


@router.post("/validation/bulk", summary="Validate several entries at once")
def bulk_validate(
    entry_ids: list[str],
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    comment: str | None = None,
):
    """Bulk sign-off for a supervisor working through a queue. Only entries assigned to
    the caller are affected; anything else is reported back rather than silently skipped."""
    principal.require("logbook.entry.validate")
    rows = db.execute(
        select(LogEntry).where(
            LogEntry.id.in_(entry_ids),
            LogEntry.tenant_id == tenant_id,
            LogEntry.deleted_at.is_(None),
        )
    ).scalars().all()

    validated, skipped = [], []
    for entry in rows:
        if entry.supervisor_id != principal.id and not principal.has(
            "logbook.entry.read.any", org_unit_id=entry.org_unit_id
        ):
            skipped.append({"id": entry.id, "reason": "not assigned to you"})
            continue
        if entry.validation_status == ValidationStatus.VALIDATED:
            skipped.append({"id": entry.id, "reason": "already validated"})
            continue
        entry.validation_status = ValidationStatus.VALIDATED
        entry.validated_by_id = principal.id
        entry.validated_at = utcnow()
        entry.validator_comment = comment
        db.add(entry)
        db.add(
            LogEntryAudit(
                tenant_id=tenant_id, log_entry_id=entry.id, actor_id=principal.id,
                action="validation", to_status=ValidationStatus.VALIDATED, comment=comment,
            )
        )
        validated.append(entry.id)

    missing = set(entry_ids) - {e.id for e in rows}
    skipped.extend({"id": mid, "reason": "not found"} for mid in missing)

    audit.record(db, action=AuditAction.VALIDATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="log_entry", summary=f"Bulk validated {len(validated)} entries", **meta)
    db.commit()
    return {"validated": validated, "validated_count": len(validated), "skipped": skipped}


@router.delete("/{entry_id}", summary="Withdraw an entry")
def delete_entry(entry_id: str, db: DbSession, principal: CurrentPrincipal,
                 tenant_id: TenantId, meta: ClientMeta):
    entry = db.get(LogEntry, entry_id)
    if entry is None or entry.tenant_id != tenant_id or entry.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Logbook entry not found.")
    enrolment = db.get(Enrolment, entry.enrolment_id)
    if enrolment.trainee_id != principal.id and not principal.has("logbook.entry.read.any"):
        raise HTTPException(status_code=403, detail="You cannot withdraw this entry.")
    if entry.validation_status == ValidationStatus.VALIDATED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Validated entries cannot be withdrawn; they form part of the "
                   "trainee's evidentiary record.",
        )
    entry.deleted_at = utcnow()
    db.add(entry)
    db.add(LogEntryAudit(tenant_id=tenant_id, log_entry_id=entry.id, actor_id=principal.id,
                         action="withdrawn", from_status=entry.validation_status))
    audit.record(db, action=AuditAction.DELETE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="log_entry", entity_id=entry.id, summary="Withdrew logbook entry", **meta)
    db.commit()
    return {"detail": "Entry withdrawn."}


@router.get("/{entry_id}/history", summary="Audit trail for an entry")
def entry_history(entry_id: str, db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    entry = db.get(LogEntry, entry_id)
    if entry is None or entry.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Logbook entry not found.")
    if not _can_read_entry(db, principal, entry):
        raise HTTPException(status_code=403, detail="You cannot view this logbook entry.")
    rows = db.execute(
        select(LogEntryAudit)
        .where(LogEntryAudit.log_entry_id == entry_id)
        .order_by(LogEntryAudit.created_at.asc())
    ).scalars().all()
    return [
        {
            "id": r.id,
            "action": r.action,
            "actor_id": r.actor_id,
            "from_status": r.from_status,
            "to_status": r.to_status,
            "comment": r.comment,
            "changes": r.changes,
            "at": r.created_at,
        }
        for r in rows
    ]
