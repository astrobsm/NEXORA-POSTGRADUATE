"""Multi-tenancy: institutions and the eight-level organisational ladder."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SoftDeleteMixin, SyncMixin, TimestampMixin
from app.models.enums import Discipline, OrgKind

if TYPE_CHECKING:
    from app.models.curriculum import Programme, Specialty
    from app.models.identity import User


class Tenant(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """An institution: a teaching hospital, FMC, college, university or national body."""

    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("code", name="uq_tenants_code"),
        UniqueConstraint("slug", name="uq_tenants_slug"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default=OrgKind.HOSPITAL, nullable=False)

    country: Mapped[str] = mapped_column(String(2), default="NG", nullable=False)
    state: Mapped[str | None] = mapped_column(String(80), default=None)
    city: Mapped[str | None] = mapped_column(String(80), default=None)
    address: Mapped[str | None] = mapped_column(Text, default=None)
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Lagos", nullable=False)
    locale: Mapped[str] = mapped_column(String(16), default="en-NG", nullable=False)

    contact_email: Mapped[str | None] = mapped_column(String(255), default=None)
    contact_phone: Mapped[str | None] = mapped_column(String(40), default=None)
    website: Mapped[str | None] = mapped_column(String(255), default=None)
    logo_key: Mapped[str | None] = mapped_column(String(512), default=None)

    parent_tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), default=None, index=True
    )

    #: Accrediting bodies this institution reports to, e.g. ``["npmcn", "mdcn"]``.
    accrediting_bodies: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    #: Free-form institutional configuration. Anything here is editable through the
    #: admin console and is deliberately *not* modelled as columns, so that policy can
    #: change without a migration. See docs/CONFIGURATION.md for the recognised keys.
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    #: Visual identity for white-labelling (primary/accent colours, typography).
    branding: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    org_units: Mapped[list[OrgUnit]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan", passive_deletes=True
    )
    users: Mapped[list[User]] = relationship(back_populates="tenant", passive_deletes=True)


class OrgUnit(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    """A node in the National → College → Hospital → Faculty → Department → Unit →
    Subspecialty → Programme ladder.

    Modelled as a single self-referential tree rather than eight tables so that an
    institution can use as few or as many levels as it needs. ``path`` is a
    materialised ancestor path (``/root/faculty/department``) enabling subtree queries
    with a single ``LIKE 'path/%'`` predicate on both SQLite and PostgreSQL.
    """

    __tablename__ = "org_units"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_org_units_tenant_code"),
        Index("ix_org_units_tenant_path", "tenant_id", "path"),
        Index("ix_org_units_tenant_kind", "tenant_id", "kind"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="CASCADE"), default=None, index=True
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(64), default=None)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    depth: Mapped[int] = mapped_column(default=0, nullable=False)
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)

    discipline: Mapped[str] = mapped_column(String(16), default=Discipline.MEDICAL, nullable=False)
    specialty_id: Mapped[str | None] = mapped_column(
        ForeignKey("specialties.id", ondelete="SET NULL"), default=None, index=True
    )
    head_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    description: Mapped[str | None] = mapped_column(Text, default=None)
    #: Capacity and infrastructure figures used by the accreditation module
    #: (beds, theatres, ICU beds, clinics per week, library seats, ...).
    capacity: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    tenant: Mapped[Tenant] = relationship(back_populates="org_units")
    parent: Mapped[OrgUnit | None] = relationship(
        back_populates="children", remote_side="OrgUnit.id"
    )
    children: Mapped[list[OrgUnit]] = relationship(
        back_populates="parent", cascade="all, delete-orphan", passive_deletes=True
    )
    specialty: Mapped[Specialty | None] = relationship(foreign_keys=[specialty_id])
    programmes: Mapped[list[Programme]] = relationship(
        back_populates="org_unit", passive_deletes=True
    )

    # -- helpers ----------------------------------------------------------
    def compute_path(self) -> str:
        """Path is assembled by the service layer on insert/move."""
        if self.parent is None:
            return f"/{self.code}"
        return f"{self.parent.path}/{self.code}"

    @property
    def subtree_prefix(self) -> str:
        return f"{self.path}/"

    def is_ancestor_of(self, other: OrgUnit) -> bool:
        return other.path.startswith(self.subtree_prefix)

    def covers(self, other: OrgUnit) -> bool:
        """True when a permission held at *self* should also apply at *other*."""
        return other.id == self.id or self.is_ancestor_of(other)


class TenantIntegration(Base, IdMixin, TimestampMixin):
    """Per-institution external system bindings: LDAP/AD, SSO, SMTP, SMS, S3."""

    __tablename__ = "tenant_integrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_tenant_integration_provider"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)  # ldap | oidc | smtp | sms | s3
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Non-secret configuration. Secrets are stored encrypted in ``secret_ref`` which
    #: points at the deployment's secret manager rather than the database.
    config: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(String(255), default=None)
