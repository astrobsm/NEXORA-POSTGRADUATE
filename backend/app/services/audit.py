"""Audit trail writing, with secret redaction."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.system import AuditLog

#: Field names whose values are never written to the audit trail.
REDACTED_FIELDS = frozenset(
    {
        "hashed_password",
        "password",
        "new_password",
        "mfa_secret",
        "mfa_recovery_hashes",
        "secret_ref",
        "access_token",
        "refresh_token",
        "token",
        "api_key",
        "secret_key",
    }
)

REDACTION = "«redacted»"


def redact(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    return {
        key: (REDACTION if key.lower() in REDACTED_FIELDS else value)
        for key, value in payload.items()
    }


def diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """Field-level change set, redacted, skipping unchanged values."""
    before, after = before or {}, after or {}
    changes: dict[str, Any] = {}
    for key in set(before) | set(after):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if key.lower() in REDACTED_FIELDS:
            changes[key] = {"from": REDACTION, "to": REDACTION}
        else:
            changes[key] = {"from": old, "to": new}
    return changes


def record(
    db: Session,
    *,
    action: str,
    tenant_id: str | None = None,
    actor_id: str | None = None,
    actor_label: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    summary: str | None = None,
    changes: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    via_sync: bool = False,
    succeeded: bool = True,
) -> AuditLog:
    """Append an audit entry. Never raises into the caller's transaction path."""
    entry = AuditLog(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_label=actor_label,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=(summary or "")[:500] or None,
        changes=redact(changes),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:512] or None,
        request_id=request_id,
        via_sync=via_sync,
        succeeded=succeeded,
    )
    db.add(entry)
    return entry
