"""The notification engine.

Events are raised by domain code; ``NotificationRule`` rows decide who hears about them,
through which channel, and how far in advance. Institutions edit rules; nobody edits
code to change who gets reminded about a proposal deadline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import owned_or_shared, utcnow
from app.models.enums import NotificationChannel, NotificationPriority
from app.models.system import Notification, NotificationRule, NotificationTemplate


#: Well-known event codes. Institutions may add their own; unknown codes simply have no
#: default template and fall back to the payload's title/body.
class Events:
    ROTATION_STARTING = "rotation.starting"
    ROTATION_ENDING = "rotation.ending"
    ROTATION_SIGNOFF_DUE = "rotation.signoff_due"
    DUTY_SHIFT_TOMORROW = "duty.shift_tomorrow"
    DUTY_MISSED = "duty.missed"
    DUTY_SWAP_REQUESTED = "duty.swap_requested"
    LOGBOOK_VALIDATION_PENDING = "logbook.validation_pending"
    LOGBOOK_ENTRY_QUERIED = "logbook.entry_queried"
    LOGBOOK_ENTRY_VALIDATED = "logbook.entry_validated"
    ASSESSMENT_DUE = "assessment.due"
    ASSESSMENT_RECEIVED = "assessment.received"
    ACTIVITY_SCHEDULED = "academic.activity_scheduled"
    ACTIVITY_REMINDER = "academic.activity_reminder"
    ATTENDANCE_BELOW_THRESHOLD = "academic.attendance_below_threshold"
    RESEARCH_MILESTONE_DUE = "research.milestone_due"
    RESEARCH_PROPOSAL_DEADLINE = "research.proposal_deadline"
    SUPERVISION_MEETING_DUE = "research.supervision_meeting_due"
    EXAM_REGISTRATION_OPEN = "exam.registration_open"
    EXAM_ELIGIBILITY_AT_RISK = "exam.eligibility_at_risk"
    CME_ASSIGNMENT_DUE = "cme.assignment_due"
    LEAVE_DECISION = "training.leave_decision"
    PROMOTION_REVIEW_READY = "promotion.review_ready"
    PROMOTION_DECISION = "promotion.decision"
    ACCREDITATION_GAP = "accreditation.gap_detected"
    TRAINEE_AT_RISK = "analytics.trainee_at_risk"


DEFAULT_TEMPLATES: dict[str, tuple[str, str]] = {
    Events.ROTATION_STARTING: (
        "Rotation starting: {{rotation_name}}",
        "Your {{rotation_name}} rotation at {{org_unit_name}} starts on {{start_date}}. "
        "Your supervisor is {{supervisor_name}}.",
    ),
    Events.ROTATION_ENDING: (
        "Rotation ending: {{rotation_name}}",
        "{{rotation_name}} ends on {{end_date}}. {{outstanding_count}} requirement(s) "
        "remain outstanding — review them before requesting sign-off.",
    ),
    Events.ROTATION_SIGNOFF_DUE: (
        "Sign-off required: {{trainee_name}}",
        "{{trainee_name}} completed {{rotation_name}} on {{end_date}} and is awaiting "
        "your sign-off.",
    ),
    Events.LOGBOOK_VALIDATION_PENDING: (
        "{{count}} logbook entries awaiting validation",
        "You have {{count}} logbook entries from {{trainee_count}} trainee(s) awaiting "
        "validation, the oldest submitted {{oldest_days}} day(s) ago.",
    ),
    Events.LOGBOOK_ENTRY_QUERIED: (
        "Logbook entry returned for correction",
        "Your entry '{{entry_title}}' was returned by {{validator_name}}: "
        "{{validator_comment}}",
    ),
    Events.RESEARCH_MILESTONE_DUE: (
        "Dissertation milestone due: {{milestone_title}}",
        "'{{milestone_title}}' for '{{project_title}}' is due on {{due_date}}.",
    ),
    Events.ATTENDANCE_BELOW_THRESHOLD: (
        "Academic attendance below requirement",
        "Your attendance is {{measured}}% against a required {{target}}%. "
        "{{sessions_needed}} further session(s) are needed to reach the threshold.",
    ),
    Events.PROMOTION_REVIEW_READY: (
        "Promotion review ready: {{trainee_name}}",
        "{{trainee_name}} has cleared all promotion gates for "
        "{{from_level}} → {{to_level}} and is ready for committee review.",
    ),
    Events.TRAINEE_AT_RISK: (
        "Trainee flagged for support: {{trainee_name}}",
        "{{trainee_name}} is showing {{rag}} status with {{gap_count}} unmet mandatory "
        "requirement(s). Consider a supportive review meeting.",
    ),
    Events.CME_ASSIGNMENT_DUE: (
        "CME due: {{resource_title}}",
        "'{{resource_title}}' is due on {{due_date}} ({{credits}} credits).",
    ),
    Events.EXAM_ELIGIBILITY_AT_RISK: (
        "Examination eligibility at risk",
        "{{blocking_count}} requirement(s) currently block your eligibility for "
        "{{exam_name}}. Registration closes on {{deadline}}.",
    ),
}

_TOKEN = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass(slots=True)
class NotificationRequest:
    event_code: str
    tenant_id: str
    user_ids: list[str]
    context: dict[str, Any]
    priority: str = NotificationPriority.NORMAL
    channels: list[str] | None = None
    action_url: str | None = None
    action_label: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    scheduled_for: Any = None


def render(text: str, context: dict[str, Any]) -> str:
    """Substitute ``{{token}}`` placeholders; unknown tokens render as an em dash so a
    missing value never leaks braces into a message a clinician reads."""
    def _replace(match: re.Match[str]) -> str:
        value = context.get(match.group(1))
        return "—" if value is None else str(value)

    return _TOKEN.sub(_replace, text)


def resolve_template(
    db: Session, tenant_id: str, event_code: str, channel: str
) -> tuple[str, str]:
    """Institution template if one exists, otherwise the platform default."""
    row = db.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.event_code == event_code,
            NotificationTemplate.channel == channel,
            NotificationTemplate.is_active.is_(True),
            owned_or_shared(NotificationTemplate.tenant_id, tenant_id),
        ).order_by(NotificationTemplate.tenant_id.is_(None))
    ).scalars().first()
    if row:
        return row.subject or event_code, row.body
    return DEFAULT_TEMPLATES.get(event_code, (event_code.replace(".", " ").title(), "{{message}}"))


def dispatch(db: Session, request: NotificationRequest) -> list[Notification]:
    """Create notification rows for each recipient and channel.

    Actual delivery (email, SMS, web push) is performed by the worker that drains rows
    with ``delivery_status == 'pending'``; in-app notifications are considered delivered
    the moment they are written.
    """
    rules = db.execute(
        select(NotificationRule).where(
            NotificationRule.tenant_id == request.tenant_id,
            NotificationRule.event_code == request.event_code,
            NotificationRule.is_active.is_(True),
        )
    ).scalars().all()

    channels = request.channels or (
        sorted({c for rule in rules for c in (rule.channels or [])})
        or [NotificationChannel.IN_APP]
    )
    priority = request.priority
    for rule in rules:
        if rule.priority:
            priority = rule.priority

    created: list[Notification] = []
    for user_id in dict.fromkeys(request.user_ids):  # de-duplicate, preserve order
        for channel in channels:
            subject, body = resolve_template(db, request.tenant_id, request.event_code, channel)
            notification = Notification(
                tenant_id=request.tenant_id,
                user_id=user_id,
                event_code=request.event_code,
                channel=channel,
                priority=priority,
                title=render(subject, request.context)[:300],
                body=render(body, request.context),
                action_url=request.action_url,
                action_label=request.action_label,
                entity_type=request.entity_type,
                entity_id=request.entity_id,
                payload=request.context,
                scheduled_for=request.scheduled_for,
                delivery_status="sent" if channel == NotificationChannel.IN_APP else "pending",
                sent_at=utcnow() if channel == NotificationChannel.IN_APP else None,
            )
            db.add(notification)
            created.append(notification)
    return created


def due_date_for(rule: NotificationRule, reference: date) -> date:
    """When a lead-time rule should fire relative to a reference date."""
    return reference - timedelta(days=rule.lead_days)


def mark_read(db: Session, notification: Notification) -> Notification:
    if notification.read_at is None:
        notification.read_at = utcnow()
        db.add(notification)
    return notification
