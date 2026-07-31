"""Declarative base, common column types and reusable mixins."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, event, or_
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator

from app.core.security import new_id


class UtcDateTime(TypeDecorator):
    """Timezone-aware datetime that round-trips correctly on SQLite *and* PostgreSQL."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


# JSON works natively on both backends; PostgreSQL deployments are upgraded to JSONB
# by the Alembic migration so indexing/containment operators remain available.
JsonDict = JSON
JsonList = JSON


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: UtcDateTime,
        dict[str, Any]: JSON,
        list[str]: JSON,
        list[dict[str, Any]]: JSON,
    }

    def as_dict(self, *, exclude: set[str] | None = None) -> dict[str, Any]:
        exclude = exclude or set()
        out: dict[str, Any] = {}
        for column in self.__table__.columns:  # type: ignore[attr-defined]
            if column.name in exclude:
                continue
            value = getattr(self, column.name)
            if isinstance(value, (datetime, date)):
                value = value.isoformat()
            out[column.name] = value
        return out

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk!r}>"


class IdMixin:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(default=None, index=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class AuthorshipMixin:
    created_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    updated_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


class SyncMixin:
    """Fields required by the offline-first synchronisation protocol.

    ``revision`` increments on every server-side write; clients send the revision they
    last saw so the sync endpoint can detect conflicts deterministically without relying
    on wall clocks. ``client_uuid`` lets a device reconcile a record it created offline
    with the server-assigned identifier.
    """

    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    client_uuid: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    synced_at: Mapped[datetime | None] = mapped_column(default=None)


class TenantScopedMixin:
    """Every row that belongs to an institution carries its tenant id.

    All read paths filter on this column; see ``app.api.deps.tenant_scope``.
    """

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )


def tenant_index(table_name: str, *columns: str, unique: bool = False) -> Index:
    name = f"ix_{table_name}_tenant_{'_'.join(columns)}"
    return Index(name, "tenant_id", *columns, unique=unique)


def owned_or_shared(column: Any, owner_id: str | None) -> Any:
    """Match rows owned by ``owner_id`` **or** shared platform-level rows.

    Several tables use a NULL owner to mean "available to everyone": system roles,
    the platform specialty catalogue, shared accreditation standards, default
    notification templates.

    The obvious spelling — ``column.in_([owner_id, None])`` — is silently wrong.
    SQL's ``IN`` compares with ``=``, and ``NULL = NULL`` is unknown, so a row with
    a NULL owner never matches and every shared record disappears from the result.
    This helper exists so that mistake cannot be made twice.
    """
    return or_(column == owner_id, column.is_(None))


@event.listens_for(Base, "before_update", propagate=True)
def _bump_revision(mapper: Any, connection: Any, target: Any) -> None:
    if isinstance(target, SyncMixin):
        target.revision = (target.revision or 0) + 1
