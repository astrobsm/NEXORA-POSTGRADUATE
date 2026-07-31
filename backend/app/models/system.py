"""Notifications, audit trail, offline-sync bookkeeping and generated reports."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, SyncMixin, TimestampMixin
from app.models.enums import AuditAction, NotificationChannel, NotificationPriority


class NotificationTemplate(Base, IdMixin, TimestampMixin):
    """Message body per event type and channel, with token substitution."""

    __tablename__ = "notification_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "event_code", "channel", name="uq_notification_template"),
    )

    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), default=None, index=True
    )
    event_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(
        String(16), default=NotificationChannel.IN_APP, nullable=False
    )
    subject: Mapped[str | None] = mapped_column(String(300), default=None)
    #: Supports ``{{trainee_name}}``, ``{{rotation_name}}``, ``{{due_date}}`` etc.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class NotificationRule(Base, IdMixin, TimestampMixin):
    """When and to whom an event triggers a notification. Fully institution-editable."""

    __tablename__ = "notification_rules"
    __table_args__ = (Index("ix_notification_rules_event", "tenant_id", "event_code"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Role codes to notify, plus relationship targets such as "supervisor", "trainee".
    audience: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    channels: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    #: Days before the referenced date to fire; negative fires after.
    lead_days: Mapped[int] = mapped_column(default=0, nullable=False)
    #: Repeat cadence in days while the condition remains true; 0 == fire once.
    repeat_every_days: Mapped[int] = mapped_column(default=0, nullable=False)
    priority: Mapped[str] = mapped_column(
        String(16), default=NotificationPriority.NORMAL, nullable=False
    )
    #: Optional additional filter, e.g. {"training_years": [3, 4]}.
    conditions: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    quiet_hours: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Notification(Base, IdMixin, TimestampMixin, SyncMixin):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_unread", "user_id", "read_at"),
        Index("ix_notifications_tenant_event", "tenant_id", "event_code"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_code: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(
        String(16), default=NotificationChannel.IN_APP, nullable=False
    )
    priority: Mapped[str] = mapped_column(
        String(16), default=NotificationPriority.NORMAL, nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: Deep link into the app, e.g. "/logbook/entries/abc123".
    action_url: Mapped[str | None] = mapped_column(String(512), default=None)
    action_label: Mapped[str | None] = mapped_column(String(80), default=None)
    #: Entity this notification concerns, for de-duplication and grouping.
    entity_type: Mapped[str | None] = mapped_column(String(64), default=None)
    entity_id: Mapped[str | None] = mapped_column(String(32), default=None)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    scheduled_for: Mapped[datetime | None] = mapped_column(default=None, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(default=None)
    read_at: Mapped[datetime | None] = mapped_column(default=None)
    dismissed_at: Mapped[datetime | None] = mapped_column(default=None)
    delivery_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    delivery_error: Mapped[str | None] = mapped_column(Text, default=None)


class AuditLog(Base, IdMixin, TimestampMixin):
    """Append-only record of every consequential action.

    Never updated or deleted by application code; retention is enforced by an archival
    job that moves rows to cold storage.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_time", "tenant_id", "created_at"),
        Index("ix_audit_logs_actor", "actor_id", "created_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
    )

    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), default=None, index=True
    )
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    actor_label: Mapped[str | None] = mapped_column(String(200), default=None)
    action: Mapped[str] = mapped_column(String(32), default=AuditAction.UPDATE, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), default=None)
    entity_id: Mapped[str | None] = mapped_column(String(32), default=None)
    summary: Mapped[str | None] = mapped_column(String(500), default=None)
    #: Field-level diff {"field": {"from": ..., "to": ...}} with secrets redacted.
    changes: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)
    request_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    #: Set when the action originated from an offline device replay.
    via_sync: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SyncCheckpoint(Base, IdMixin, TimestampMixin):
    """Per-device high-water mark for incremental pull."""

    __tablename__ = "sync_checkpoints"
    __table_args__ = (
        UniqueConstraint("user_id", "device_id", name="uq_sync_checkpoint_device"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    device_label: Mapped[str | None] = mapped_column(String(160), default=None)
    last_pulled_at: Mapped[datetime | None] = mapped_column(default=None)
    last_pushed_at: Mapped[datetime | None] = mapped_column(default=None)
    #: {"log_entries": "2026-07-30T10:00:00Z", "assessments": ...}
    cursors: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    pending_conflicts: Mapped[int] = mapped_column(default=0, nullable=False)
    app_version: Mapped[str | None] = mapped_column(String(32), default=None)


class SyncConflict(Base, IdMixin, TimestampMixin):
    """A push rejected because the server row moved on — surfaced for user resolution."""

    __tablename__ = "sync_conflicts"
    __table_args__ = (Index("ix_sync_conflicts_user_open", "user_id", "resolved_at"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[str | None] = mapped_column(String(64), default=None)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(32), default=None)
    client_uuid: Mapped[str | None] = mapped_column(String(64), default=None)
    client_revision: Mapped[int | None] = mapped_column(default=None)
    server_revision: Mapped[int | None] = mapped_column(default=None)
    client_payload: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    server_payload: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: server_wins | client_wins | merged | manual
    resolution: Mapped[str | None] = mapped_column(String(24), default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)
    resolved_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


class GeneratedReport(Base, IdMixin, TimestampMixin):
    """A rendered export (PDF/XLSX/CSV/DOCX/PPTX) held in object storage."""

    __tablename__ = "generated_reports"
    __table_args__ = (Index("ix_generated_reports_tenant_kind", "tenant_id", "report_kind"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    report_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    format: Mapped[str] = mapped_column(String(16), default="pdf", nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    scope_type: Mapped[str | None] = mapped_column(String(32), default=None)
    scope_id: Mapped[str | None] = mapped_column(String(32), default=None)
    period_start: Mapped[date | None] = mapped_column(Date, default=None)
    period_end: Mapped[date | None] = mapped_column(Date, default=None)
    parameters: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    file_size_bytes: Mapped[int | None] = mapped_column(default=None)
    #: SHA-256 of the rendered file — lets a college verify a submitted document.
    checksum: Mapped[str | None] = mapped_column(String(128), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    expires_at: Mapped[datetime | None] = mapped_column(default=None)
