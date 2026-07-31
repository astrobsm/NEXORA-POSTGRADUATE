"""Academic activities and attendance."""

from __future__ import annotations

import secrets
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, TenantId
from app.db.base import utcnow
from app.models.academic import AcademicActivity, ActivityParticipant
from app.models.cme import CmeCreditLedger
from app.models.duty import AttendanceRecord
from app.models.enums import AttendanceStatus, AuditAction, ParticipantRole
from app.models.tenancy import OrgUnit
from app.schemas.common import Page
from app.schemas.training import (
    AcademicActivityCreate,
    AcademicActivityOut,
    ActivityParticipantOut,
    AttendanceMarkRequest,
)
from app.services import audit

router = APIRouter()

#: Participation credit multipliers — presenting is worth more than attending.
ROLE_CREDIT_MULTIPLIER: dict[str, float] = {
    ParticipantRole.ATTENDEE: 1.0,
    ParticipantRole.DISCUSSANT: 1.25,
    ParticipantRole.MODERATOR: 1.5,
    ParticipantRole.PRESENTER: 2.0,
    ParticipantRole.EXAMINER: 1.5,
    ParticipantRole.ORGANISER: 1.25,
}


def _out(db, a: AcademicActivity) -> AcademicActivityOut:
    out = AcademicActivityOut.model_validate(a)
    if a.presenter is not None:
        out.presenter_name = a.presenter.full_name
    out.attendee_count = db.execute(
        select(func.count()).select_from(ActivityParticipant).where(
            ActivityParticipant.activity_id == a.id, ActivityParticipant.attended.is_(True)
        )
    ).scalar_one()
    return out


@router.post("/activities", response_model=AcademicActivityOut,
             status_code=status.HTTP_201_CREATED, summary="Schedule an academic activity")
def create_activity(payload: AcademicActivityCreate, db: DbSession, principal: CurrentPrincipal,
                    tenant_id: TenantId, meta: ClientMeta):
    principal.require("academic.activity.manage")
    unit = db.get(OrgUnit, payload.org_unit_id)
    if unit is None or unit.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Organisational unit not found.")

    activity = AcademicActivity(
        tenant_id=tenant_id,
        scheduled_on=payload.scheduled_at.date(),
        checkin_code=secrets.token_hex(3).upper(),
        **payload.model_dump(),
    )
    db.add(activity)
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="academic_activity", entity_id=activity.id,
                 summary=f"Scheduled {payload.kind}: {payload.title}", **meta)
    db.commit()
    db.refresh(activity)
    return _out(db, activity)


@router.get("/activities", response_model=Page[AcademicActivityOut],
            summary="Academic activity calendar")
def list_activities(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    org_unit_id: str | None = None,
    kind: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    upcoming_only: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    principal.require("academic.activity.read")
    stmt = select(AcademicActivity).where(
        AcademicActivity.tenant_id == tenant_id, AcademicActivity.deleted_at.is_(None)
    )
    if org_unit_id:
        stmt = stmt.where(AcademicActivity.org_unit_id == org_unit_id)
    if kind:
        stmt = stmt.where(AcademicActivity.kind == kind)
    if date_from:
        stmt = stmt.where(AcademicActivity.scheduled_on >= date_from)
    if date_to:
        stmt = stmt.where(AcademicActivity.scheduled_on <= date_to)
    if upcoming_only:
        stmt = stmt.where(AcademicActivity.scheduled_on >= date.today())

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    order = AcademicActivity.scheduled_at.asc() if upcoming_only else AcademicActivity.scheduled_at.desc()
    rows = db.execute(
        stmt.order_by(order).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return Page[AcademicActivityOut](
        items=[_out(db, a) for a in rows], total=total, page=page, page_size=page_size
    )


@router.post("/activities/{activity_id}/attendance", summary="Record attendance")
def mark_attendance(
    activity_id: str,
    payload: AttendanceMarkRequest,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    """Record attendance for a list of users, or self check-in with the session code.

    Credits are awarded automatically at the activity's rate, multiplied by the
    participation role, and posted to the CME ledger in the same transaction so
    attendance and credit can never drift apart.
    """
    activity = db.get(AcademicActivity, activity_id)
    if activity is None or activity.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Activity not found.")

    self_checkin = bool(payload.checkin_code) and not payload.user_ids
    if self_checkin:
        if payload.checkin_code.strip().upper() != (activity.checkin_code or ""):
            raise HTTPException(status_code=403, detail="That check-in code is not valid.")
        user_ids = [principal.id]
    else:
        principal.require("academic.attendance.record")
        user_ids = payload.user_ids
        if not user_ids:
            raise HTTPException(status_code=422, detail="Provide at least one user id.")

    multiplier = ROLE_CREDIT_MULTIPLIER.get(payload.role, 1.0)
    credits = round(activity.cme_credits * multiplier, 2)
    recorded, already = [], []

    for user_id in dict.fromkeys(user_ids):
        existing = db.execute(
            select(ActivityParticipant).where(
                ActivityParticipant.activity_id == activity_id,
                ActivityParticipant.user_id == user_id,
                ActivityParticipant.role == payload.role,
            )
        ).scalar_one_or_none()

        if existing is not None:
            if existing.attended == payload.attended:
                already.append(user_id)
                continue
            participant = existing
        else:
            participant = ActivityParticipant(
                tenant_id=tenant_id, activity_id=activity_id, user_id=user_id, role=payload.role
            )
            db.add(participant)

        participant.attended = payload.attended
        participant.checked_in_at = utcnow() if payload.attended else None
        participant.minutes_present = payload.minutes_present
        participant.verified_by_id = None if self_checkin else principal.id
        participant.credits_awarded = credits if payload.attended else 0.0

        db.add(
            AttendanceRecord(
                tenant_id=tenant_id,
                user_id=user_id,
                activity_id=activity_id,
                recorded_for=activity.scheduled_on,
                status=AttendanceStatus.PRESENT if payload.attended else AttendanceStatus.ABSENT,
                check_in_at=utcnow() if payload.attended else None,
                capture_method="qr" if self_checkin else "manual",
                recorded_by_id=principal.id,
            )
        )

        if payload.attended and credits > 0:
            db.add(
                CmeCreditLedger(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    period_year=activity.scheduled_on.year,
                    source_kind="academic_activity",
                    source_id=activity.id,
                    description=f"{activity.kind.replace('_', ' ').title()}: {activity.title}",
                    credits=credits,
                    awarded_on=activity.scheduled_on,
                    awarded_by_id=principal.id,
                )
            )
        recorded.append(user_id)

    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="activity_participant", entity_id=activity_id,
                 summary=f"Attendance recorded for {len(recorded)} participant(s)", **meta)
    db.commit()
    return {
        "activity_id": activity_id,
        "recorded": len(recorded),
        "already_recorded": len(already),
        "credits_each": credits,
    }


@router.get("/activities/{activity_id}/participants",
            response_model=list[ActivityParticipantOut], summary="Activity attendance list")
def list_participants(activity_id: str, db: DbSession, principal: CurrentPrincipal,
                      tenant_id: TenantId):
    principal.require("academic.activity.read")
    rows = db.execute(
        select(ActivityParticipant).where(ActivityParticipant.activity_id == activity_id)
    ).scalars().all()
    out = []
    for row in rows:
        item = ActivityParticipantOut.model_validate(row)
        if row.user is not None:
            item.user_name = row.user.full_name
        out.append(item)
    return out


@router.get("/attendance/summary", summary="Attendance percentage for a user")
def attendance_summary(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    user_id: str | None = None,
    org_unit_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    activity_kinds: list[str] | None = Query(default=None),
):
    """Attendance measured the way the colleges phrase it: sessions attended over
    mandatory sessions held, broken down by activity type."""
    target = user_id or principal.id
    if target != principal.id:
        principal.require("academic.activity.read")

    end = date_to or date.today()
    start = date_from or (end - timedelta(days=365))

    expected_stmt = select(AcademicActivity).where(
        AcademicActivity.tenant_id == tenant_id,
        AcademicActivity.deleted_at.is_(None),
        AcademicActivity.is_mandatory.is_(True),
        AcademicActivity.scheduled_on >= start,
        AcademicActivity.scheduled_on <= end,
    )
    if org_unit_id:
        expected_stmt = expected_stmt.where(AcademicActivity.org_unit_id == org_unit_id)
    if activity_kinds:
        expected_stmt = expected_stmt.where(AcademicActivity.kind.in_(activity_kinds))
    expected = list(db.execute(expected_stmt).scalars().all())

    attended_ids = set(
        db.execute(
            select(ActivityParticipant.activity_id).where(
                ActivityParticipant.user_id == target,
                ActivityParticipant.attended.is_(True),
            )
        ).scalars().all()
    )

    by_kind: dict[str, dict[str, int]] = {}
    for activity in expected:
        bucket = by_kind.setdefault(activity.kind, {"expected": 0, "attended": 0})
        bucket["expected"] += 1
        if activity.id in attended_ids:
            bucket["attended"] += 1

    total_expected = len(expected)
    total_attended = sum(1 for a in expected if a.id in attended_ids)

    return {
        "user_id": target,
        "period": {"start": str(start), "end": str(end)},
        "expected": total_expected,
        "attended": total_attended,
        "percent": round(total_attended / total_expected * 100, 1) if total_expected else 0.0,
        "by_kind": {
            kind: {
                **counts,
                "percent": round(counts["attended"] / counts["expected"] * 100, 1)
                if counts["expected"] else 0.0,
            }
            for kind, counts in sorted(by_kind.items())
        },
    }


@router.get("/cme/ledger", summary="CME credit ledger")
def cme_ledger(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    user_id: str | None = None,
    year: int | None = None,
):
    target = user_id or principal.id
    if target != principal.id:
        principal.require("cme.assignment.manage")
    stmt = select(CmeCreditLedger).where(
        CmeCreditLedger.tenant_id == tenant_id,
        CmeCreditLedger.user_id == target,
        CmeCreditLedger.is_reversed.is_(False),
    )
    if year:
        stmt = stmt.where(CmeCreditLedger.period_year == year)
    rows = db.execute(stmt.order_by(CmeCreditLedger.awarded_on.desc())).scalars().all()

    by_year: dict[int, float] = {}
    by_source: dict[str, float] = {}
    for row in rows:
        by_year[row.period_year] = round(by_year.get(row.period_year, 0.0) + row.credits, 2)
        by_source[row.source_kind] = round(by_source.get(row.source_kind, 0.0) + row.credits, 2)

    return {
        "user_id": target,
        "total_credits": round(sum(r.credits for r in rows), 2),
        "by_year": by_year,
        "by_source": by_source,
        "entries": [
            {
                "id": r.id,
                "description": r.description,
                "credits": r.credits,
                "awarded_on": r.awarded_on,
                "source_kind": r.source_kind,
                "recognised_by": r.recognised_by,
            }
            for r in rows[:200]
        ],
    }
