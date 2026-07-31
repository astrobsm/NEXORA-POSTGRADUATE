"""Reference data and notifications.

A single place the client can fetch every vocabulary it needs to render forms and
filters, so the frontend never hard-codes a domain list that the backend owns.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from app.api.deps import CurrentPrincipal, DbSession, TenantId
from app.models import enums
from app.models.system import Notification, NotificationRule
from app.services import notifications as notification_service

router = APIRouter()


def _values(enum_cls) -> list[dict[str, str]]:
    return [
        {"value": member.value, "label": member.value.replace("_", " ").capitalize()}
        for member in enum_cls
    ]


@router.get("/vocabularies", summary="Every enumeration the client renders")
def vocabularies():
    """Public reference data. Deliberately unauthenticated so the sign-in screen and
    offline shell can render before a session exists."""
    return {
        "org_kinds": _values(enums.OrgKind),
        "accrediting_bodies": _values(enums.AccreditingBody),
        "disciplines": _values(enums.Discipline),
        "programme_types": _values(enums.ProgrammeType),
        "training_levels": _values(enums.TrainingLevel),
        "curriculum_statuses": _values(enums.CurriculumStatus),
        "competency_domains": _values(enums.CompetencyDomain),
        "entrustment_levels": [
            {"value": m.value, "label": m.value.split("_", 1)[1].replace("_", " ").capitalize(),
             "rank": enums.ENTRUSTMENT_ORDER[m.value]}
            for m in enums.EntrustmentLevel
        ],
        "requirement_kinds": _values(enums.RequirementKind),
        "requirement_operators": _values(enums.RequirementOperator),
        "requirement_scopes": _values(enums.RequirementScope),
        "requirement_severities": _values(enums.RequirementSeverity),
        "enrolment_statuses": _values(enums.EnrolmentStatus),
        "rotation_statuses": _values(enums.RotationStatus),
        "leave_types": _values(enums.LeaveType),
        "approval_statuses": _values(enums.ApprovalStatus),
        "duty_kinds": _values(enums.DutyKind),
        "attendance_statuses": _values(enums.AttendanceStatus),
        "log_entry_types": _values(enums.LogEntryType),
        "participation_roles": [
            {"value": m.value, "label": m.value.replace("_", " ").capitalize(),
             "weight": enums.PARTICIPATION_WEIGHT[m.value]}
            for m in enums.ParticipationRole
        ],
        "case_complexities": _values(enums.CaseComplexity),
        "case_outcomes": _values(enums.CaseOutcome),
        "validation_statuses": _values(enums.ValidationStatus),
        "assessment_kinds": _values(enums.AssessmentKind),
        "assessment_verdicts": _values(enums.AssessmentVerdict),
        "academic_activity_kinds": _values(enums.AcademicActivityKind),
        "participant_roles": _values(enums.ParticipantRole),
        "question_types": _values(enums.QuestionType),
        "exam_modes": _values(enums.ExamMode),
        "cme_resource_kinds": _values(enums.CmeResourceKind),
        "research_types": _values(enums.ResearchType),
        "dissertation_stages": [
            {"value": m.value, "label": m.value.replace("_", " ").capitalize(),
             "index": index}
            for index, m in enumerate(enums.DissertationStage)
        ],
        "publication_types": _values(enums.PublicationType),
        "score_domains": _values(enums.ScoreDomain),
        "rag_statuses": _values(enums.RagStatus),
        "promotion_outcomes": _values(enums.PromotionOutcome),
        "notification_channels": _values(enums.NotificationChannel),
        "notification_events": sorted(
            code for name, code in vars(notification_service.Events).items()
            if not name.startswith("_") and isinstance(code, str)
        ),
    }


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
@router.get("/notifications", summary="Your notifications")
def list_notifications(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    unread_only: bool = False,
    limit: int = Query(50, ge=1, le=200),
):
    stmt = select(Notification).where(
        Notification.tenant_id == tenant_id,
        Notification.user_id == principal.id,
        Notification.channel == enums.NotificationChannel.IN_APP,
        Notification.dismissed_at.is_(None),
    )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    rows = db.execute(stmt.order_by(Notification.created_at.desc()).limit(limit)).scalars().all()
    unread = db.execute(
        select(func.count()).select_from(Notification).where(
            Notification.user_id == principal.id,
            Notification.read_at.is_(None),
            Notification.dismissed_at.is_(None),
            Notification.channel == enums.NotificationChannel.IN_APP,
        )
    ).scalar_one()
    return {
        "unread_count": int(unread),
        "items": [
            {
                "id": n.id, "event_code": n.event_code, "priority": n.priority,
                "title": n.title, "body": n.body, "action_url": n.action_url,
                "action_label": n.action_label, "entity_type": n.entity_type,
                "entity_id": n.entity_id, "read_at": n.read_at, "created_at": n.created_at,
            }
            for n in rows
        ],
    }


@router.post("/notifications/{notification_id}/read", summary="Mark as read")
def mark_read(notification_id: str, db: DbSession, principal: CurrentPrincipal):
    notification = db.get(Notification, notification_id)
    if notification is None or notification.user_id != principal.id:
        raise HTTPException(status_code=404, detail="Notification not found.")
    notification_service.mark_read(db, notification)
    db.commit()
    return {"id": notification.id, "read_at": notification.read_at}


@router.post("/notifications/read-all", summary="Mark everything as read")
def mark_all_read(db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    rows = db.execute(
        select(Notification).where(
            Notification.user_id == principal.id,
            Notification.tenant_id == tenant_id,
            Notification.read_at.is_(None),
        )
    ).scalars().all()
    for row in rows:
        notification_service.mark_read(db, row)
    db.commit()
    return {"marked": len(rows)}


@router.get("/notification-rules", summary="Institution notification rules")
def list_rules(db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    principal.require("system.notification.manage")
    rows = db.execute(
        select(NotificationRule).where(NotificationRule.tenant_id == tenant_id)
        .order_by(NotificationRule.event_code)
    ).scalars().all()
    return [
        {
            "id": r.id, "event_code": r.event_code, "name": r.name, "audience": r.audience,
            "channels": r.channels, "lead_days": r.lead_days,
            "repeat_every_days": r.repeat_every_days, "priority": r.priority,
            "conditions": r.conditions, "is_active": r.is_active,
        }
        for r in rows
    ]


@router.post("/notification-rules", summary="Create or update a notification rule")
def upsert_rule(payload: dict, db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    """Institutions decide who is told what, and when. No deployment involved."""
    principal.require("system.notification.manage")
    if not payload.get("event_code") or not payload.get("name"):
        raise HTTPException(status_code=422, detail="'event_code' and 'name' are required.")

    rule = db.get(NotificationRule, payload["id"]) if payload.get("id") else None
    if rule is None:
        rule = NotificationRule(tenant_id=tenant_id, event_code=payload["event_code"],
                                name=payload["name"])
        db.add(rule)
    for field in ("name", "audience", "channels", "lead_days", "repeat_every_days",
                  "priority", "conditions", "quiet_hours", "is_active"):
        if field in payload:
            setattr(rule, field, payload[field])
    db.commit()
    db.refresh(rule)
    return {"id": rule.id, "event_code": rule.event_code, "name": rule.name}
