"""Offline synchronisation.

The client holds an IndexedDB mirror and queues writes while offline. This module
implements the two halves of the protocol:

``GET /sync/pull``
    Everything in the caller's scope that changed after their last cursor.

``POST /sync/push``
    A batch of client mutations. Each carries the ``revision`` the device last saw;
    if the server row has moved on, the push is recorded as a conflict rather than
    silently overwriting a colleague's edit. Clinical records are too important for
    last-write-wins.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, TenantId
from app.core.config import settings
from app.db.base import owned_or_shared, utcnow
from app.models.academic import AcademicActivity
from app.models.assessment import Assessment, AssessmentTemplate
from app.models.cme import CmeAssignment
from app.models.curriculum import (
    Competency,
    CurriculumVersion,
    ProcedureCatalogueItem,
    RequirementRule,
    RotationTemplate,
    TrainingYear,
)
from app.models.duty import DutyShift
from app.models.enums import AuditAction
from app.models.logbook import LogEntry
from app.models.system import Notification, SyncCheckpoint, SyncConflict
from app.models.training import Enrolment, RotationAssignment
from app.services import audit

router = APIRouter()


#: Collections the client mirrors, in dependency order so a fresh device can build its
#: local database without dangling references.
PULL_COLLECTIONS: dict[str, Any] = {
    "curriculum_versions": CurriculumVersion,
    "training_years": TrainingYear,
    "rotation_templates": RotationTemplate,
    "competencies": Competency,
    "requirement_rules": RequirementRule,
    "procedure_catalogue": ProcedureCatalogueItem,
    "assessment_templates": AssessmentTemplate,
    "enrolments": Enrolment,
    "rotation_assignments": RotationAssignment,
    "log_entries": LogEntry,
    "assessments": Assessment,
    "academic_activities": AcademicActivity,
    "duty_shifts": DutyShift,
    "cme_assignments": CmeAssignment,
    "notifications": Notification,
}

#: Only these may be written from a device. Everything else is server-authoritative.
PUSHABLE = {"log_entries": LogEntry, "assessments": Assessment}


class PushItem(BaseModel):
    collection: str
    #: Server id when updating; absent for a record created offline.
    id: str | None = None
    client_uuid: str | None = None
    #: Revision the device last saw. Omit for creates.
    base_revision: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    #: create | update
    op: str = "create"


class PushRequest(BaseModel):
    device_id: str
    device_label: str | None = None
    app_version: str | None = None
    items: list[PushItem] = Field(default_factory=list)


def _scope_filter(stmt, model, principal, tenant_id: str):
    """Restrict a pull to what this caller is entitled to hold on a device."""
    if hasattr(model, "tenant_id"):
        stmt = stmt.where(owned_or_shared(model.tenant_id, tenant_id))

    own_enrolments = select(Enrolment.id).where(Enrolment.trainee_id == principal.id)

    if model is Enrolment:
        if not principal.has("training.enrolment.read"):
            stmt = stmt.where(
                (Enrolment.trainee_id == principal.id)
                | (Enrolment.primary_supervisor_id == principal.id)
            )
    elif model is LogEntry:
        if not principal.has("logbook.entry.read.any"):
            stmt = stmt.where(
                LogEntry.enrolment_id.in_(own_enrolments)
                | (LogEntry.supervisor_id == principal.id)
            )
    elif model is Assessment:
        if not principal.has("assessment.read.any"):
            stmt = stmt.where(
                Assessment.enrolment_id.in_(own_enrolments)
                | (Assessment.assessor_id == principal.id)
            )
    elif model is RotationAssignment:
        if not principal.has("training.rotation.read"):
            stmt = stmt.where(
                RotationAssignment.enrolment_id.in_(own_enrolments)
                | (RotationAssignment.supervisor_id == principal.id)
            )
    elif model is DutyShift:
        stmt = stmt.where(DutyShift.user_id == principal.id)
    elif model is Notification:
        stmt = stmt.where(Notification.user_id == principal.id)
    elif model is CmeAssignment:
        stmt = stmt.where(CmeAssignment.user_id == principal.id)
    return stmt


@router.get("/pull", summary="Pull changes since a cursor")
def pull(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    device_id: str = Query(...),
    since: datetime | None = None,
    collections: list[str] | None = Query(default=None),
    limit: int = Query(default=0, ge=0, le=5000),
):
    principal.require("system.sync")
    page_size = limit or settings.sync_page_size
    wanted = collections or list(PULL_COLLECTIONS)
    unknown = set(wanted) - set(PULL_COLLECTIONS)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown collection(s): {', '.join(sorted(unknown))}. "
                   f"Available: {', '.join(PULL_COLLECTIONS)}.",
        )

    checkpoint = db.execute(
        select(SyncCheckpoint).where(
            SyncCheckpoint.user_id == principal.id, SyncCheckpoint.device_id == device_id
        )
    ).scalar_one_or_none()
    if checkpoint is None:
        checkpoint = SyncCheckpoint(
            tenant_id=tenant_id, user_id=principal.id, device_id=device_id, cursors={}
        )
        db.add(checkpoint)

    payload: dict[str, list[dict]] = {}
    has_more = False
    server_time = utcnow()

    for name in wanted:
        model = PULL_COLLECTIONS[name]
        cursor = since
        if cursor is None and checkpoint.cursors.get(name):
            cursor = datetime.fromisoformat(checkpoint.cursors[name])

        stmt = select(model)
        stmt = _scope_filter(stmt, model, principal, tenant_id)
        if cursor is not None and hasattr(model, "updated_at"):
            stmt = stmt.where(model.updated_at > cursor)
        if hasattr(model, "updated_at"):
            stmt = stmt.order_by(model.updated_at.asc())

        rows = db.execute(stmt.limit(page_size + 1)).scalars().all()
        if len(rows) > page_size:
            has_more = True
            rows = rows[:page_size]

        payload[name] = [row.as_dict() for row in rows]
        if rows and hasattr(rows[-1], "updated_at"):
            checkpoint.cursors[name] = rows[-1].updated_at.isoformat()

    checkpoint.last_pulled_at = server_time
    # Reassign so SQLAlchemy notices the mutation of the JSON column.
    checkpoint.cursors = dict(checkpoint.cursors)
    db.add(checkpoint)
    db.commit()

    return {
        "server_time": server_time,
        "device_id": device_id,
        "cursors": checkpoint.cursors,
        "has_more": has_more,
        "counts": {name: len(items) for name, items in payload.items()},
        "data": payload,
    }


@router.post("/push", summary="Push offline changes")
def push(
    payload: PushRequest,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    """Apply a batch of device-originated writes.

    Creates are idempotent on ``client_uuid``, so a retried batch after a flaky
    connection never duplicates a logbook entry. Updates are optimistic: a mismatched
    ``base_revision`` produces a conflict record for the user to resolve, and the server
    row is left untouched.
    """
    principal.require("system.sync")

    applied: list[dict] = []
    conflicts: list[dict] = []
    rejected: list[dict] = []

    own_enrolment_ids = {
        e.id
        for e in db.execute(
            select(Enrolment).where(Enrolment.trainee_id == principal.id)
        ).scalars()
    }

    for item in payload.items:
        model = PUSHABLE.get(item.collection)
        if model is None:
            rejected.append(
                {
                    "client_uuid": item.client_uuid,
                    "collection": item.collection,
                    "reason": f"'{item.collection}' is server-authoritative and cannot be "
                              f"written from a device.",
                }
            )
            continue

        if item.op == "create":
            existing = None
            if item.client_uuid:
                existing = db.execute(
                    select(model).where(model.client_uuid == item.client_uuid)
                ).scalar_one_or_none()
            if existing is not None:
                applied.append({"client_uuid": item.client_uuid, "id": existing.id,
                                "status": "already_applied", "revision": existing.revision})
                continue

            data = dict(item.data)
            data.pop("id", None)
            data.pop("revision", None)
            enrolment_id = data.get("enrolment_id")
            if model is LogEntry and enrolment_id not in own_enrolment_ids:
                rejected.append({"client_uuid": item.client_uuid,
                                 "reason": "You may only create logbook entries on your own enrolment."})
                continue

            try:
                record = model(tenant_id=tenant_id, client_uuid=item.client_uuid,
                               synced_at=utcnow(), **data)
            except TypeError as exc:
                rejected.append({"client_uuid": item.client_uuid,
                                 "reason": f"Unrecognised field in payload: {exc}"})
                continue
            db.add(record)
            db.flush()
            applied.append({"client_uuid": item.client_uuid, "id": record.id,
                            "status": "created", "revision": record.revision})

        elif item.op == "update":
            if not item.id:
                rejected.append({"client_uuid": item.client_uuid,
                                 "reason": "An update needs the server id."})
                continue
            record = db.get(model, item.id)
            if record is None or record.tenant_id != tenant_id:
                rejected.append({"id": item.id, "reason": "Record not found."})
                continue

            if item.base_revision is not None and record.revision != item.base_revision:
                conflict = SyncConflict(
                    tenant_id=tenant_id,
                    user_id=principal.id,
                    device_id=payload.device_id,
                    entity_type=item.collection,
                    entity_id=record.id,
                    client_uuid=item.client_uuid,
                    client_revision=item.base_revision,
                    server_revision=record.revision,
                    client_payload=item.data,
                    server_payload=record.as_dict(),
                )
                db.add(conflict)
                conflicts.append(
                    {
                        "id": record.id,
                        "conflict_id": None,
                        "client_revision": item.base_revision,
                        "server_revision": record.revision,
                        "server_payload": record.as_dict(),
                        "message": "This record was changed on the server after your device "
                                   "last saw it. Review both versions before resaving.",
                    }
                )
                continue

            for field, value in item.data.items():
                if field in {"id", "tenant_id", "revision", "created_at"}:
                    continue
                if hasattr(record, field):
                    setattr(record, field, value)
            record.synced_at = utcnow()
            db.add(record)
            db.flush()
            applied.append({"id": record.id, "status": "updated", "revision": record.revision})
        else:
            rejected.append({"client_uuid": item.client_uuid,
                             "reason": f"Unsupported operation '{item.op}'."})

    checkpoint = db.execute(
        select(SyncCheckpoint).where(
            SyncCheckpoint.user_id == principal.id, SyncCheckpoint.device_id == payload.device_id
        )
    ).scalar_one_or_none()
    if checkpoint is None:
        checkpoint = SyncCheckpoint(
            tenant_id=tenant_id, user_id=principal.id, device_id=payload.device_id, cursors={}
        )
        db.add(checkpoint)
    checkpoint.last_pushed_at = utcnow()
    checkpoint.device_label = payload.device_label or checkpoint.device_label
    checkpoint.app_version = payload.app_version or checkpoint.app_version
    checkpoint.pending_conflicts = len(conflicts)

    audit.record(db, action=AuditAction.SYNC, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="sync", summary=f"Sync push: {len(applied)} applied, "
                 f"{len(conflicts)} conflicts, {len(rejected)} rejected",
                 via_sync=True, **meta)
    db.commit()

    return {
        "server_time": utcnow(),
        "applied": applied,
        "conflicts": conflicts,
        "rejected": rejected,
        "summary": {"applied": len(applied), "conflicts": len(conflicts),
                    "rejected": len(rejected)},
    }


@router.get("/conflicts", summary="Unresolved sync conflicts")
def list_conflicts(db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    rows = db.execute(
        select(SyncConflict).where(
            SyncConflict.user_id == principal.id,
            SyncConflict.tenant_id == tenant_id,
            SyncConflict.resolved_at.is_(None),
        ).order_by(SyncConflict.created_at.desc())
    ).scalars().all()
    return [
        {
            "id": c.id, "entity_type": c.entity_type, "entity_id": c.entity_id,
            "client_revision": c.client_revision, "server_revision": c.server_revision,
            "client_payload": c.client_payload, "server_payload": c.server_payload,
            "created_at": c.created_at,
        }
        for c in rows
    ]


@router.post("/conflicts/{conflict_id}/resolve", summary="Resolve a sync conflict")
def resolve_conflict(
    conflict_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    resolution: str = Query(description="server_wins | client_wins"),
):
    conflict = db.get(SyncConflict, conflict_id)
    if conflict is None or conflict.user_id != principal.id:
        raise HTTPException(status_code=404, detail="Conflict not found.")
    if resolution not in {"server_wins", "client_wins"}:
        raise HTTPException(status_code=422,
                            detail="Resolution must be 'server_wins' or 'client_wins'.")

    if resolution == "client_wins":
        model = PUSHABLE.get(conflict.entity_type)
        record = db.get(model, conflict.entity_id) if model else None
        if record is None:
            raise HTTPException(status_code=404, detail="The underlying record no longer exists.")
        for field, value in (conflict.client_payload or {}).items():
            if field in {"id", "tenant_id", "revision", "created_at"}:
                continue
            if hasattr(record, field):
                setattr(record, field, value)
        db.add(record)

    conflict.resolution = resolution
    conflict.resolved_at = utcnow()
    conflict.resolved_by_id = principal.id
    db.add(conflict)
    db.commit()
    return {"id": conflict.id, "resolution": resolution}
