"""Users, roles, permissions and scoped role assignments."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Date, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SoftDeleteMixin, SyncMixin, TimestampMixin
from app.models.enums import Discipline, UserStatus

if TYPE_CHECKING:
    from app.models.tenancy import OrgUnit, Tenant
    from app.models.training import Enrolment


class User(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        Index("ix_users_tenant_status", "tenant_id", "status"),
        Index("ix_users_tenant_name", "tenant_id", "last_name", "first_name"),
    )

    #: NULL for platform-level accounts (National Super Administrator).
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), default=None, index=True
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    hashed_password: Mapped[str | None] = mapped_column(String(255), default=None)

    title: Mapped[str | None] = mapped_column(String(32), default=None)   # Dr / Prof / Mr
    first_name: Mapped[str] = mapped_column(String(120), nullable=False)
    middle_name: Mapped[str | None] = mapped_column(String(120), default=None)
    last_name: Mapped[str] = mapped_column(String(120), nullable=False)
    other_names: Mapped[str | None] = mapped_column(String(255), default=None)

    phone: Mapped[str | None] = mapped_column(String(40), default=None)
    alt_phone: Mapped[str | None] = mapped_column(String(40), default=None)
    date_of_birth: Mapped[date | None] = mapped_column(Date, default=None)
    gender: Mapped[str | None] = mapped_column(String(32), default=None)
    photo_key: Mapped[str | None] = mapped_column(String(512), default=None)

    # -- professional registration ---------------------------------------
    discipline: Mapped[str] = mapped_column(String(16), default=Discipline.MEDICAL, nullable=False)
    #: MDCN / dental council folio number.
    registration_number: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    registration_expiry: Mapped[date | None] = mapped_column(Date, default=None)
    #: Qualifications: [{"award": "MBBS", "institution": "...", "year": 2016}, ...]
    qualifications: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    #: Postgraduate college memberships/fellowships.
    college_memberships: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    staff_number: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    appointment_date: Mapped[date | None] = mapped_column(Date, default=None)

    # -- account state ----------------------------------------------------
    status: Mapped[str] = mapped_column(String(16), default=UserStatus.INVITED, nullable=False)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(default=None)
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)
    failed_login_count: Mapped[int] = mapped_column(default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(default=None)
    password_changed_at: Mapped[datetime | None] = mapped_column(default=None)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # -- MFA ---------------------------------------------------------------
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_secret: Mapped[str | None] = mapped_column(String(64), default=None)
    mfa_recovery_hashes: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    # -- external identity -------------------------------------------------
    external_idp: Mapped[str | None] = mapped_column(String(64), default=None)
    external_subject: Mapped[str | None] = mapped_column(String(255), default=None, index=True)

    # -- preferences -------------------------------------------------------
    #: theme, locale, notification channel opt-ins, dashboard layout.
    preferences: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    bio: Mapped[str | None] = mapped_column(Text, default=None)

    tenant: Mapped[Tenant | None] = relationship(back_populates="users", foreign_keys=[tenant_id])
    role_assignments: Mapped[list[RoleAssignment]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="RoleAssignment.user_id",
    )
    enrolments: Mapped[list[Enrolment]] = relationship(
        back_populates="trainee", foreign_keys="Enrolment.trainee_id", passive_deletes=True
    )

    # -- helpers -----------------------------------------------------------
    @property
    def full_name(self) -> str:
        parts = [self.title, self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p)

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def initials(self) -> str:
        return f"{self.first_name[:1]}{self.last_name[:1]}".upper()


class Permission(Base, IdMixin, TimestampMixin):
    """Canonical permission vocabulary, mirrored from ``app.core.rbac`` at seed time."""

    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("code", name="uq_permissions_code"),)

    code: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class Role(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """A named bundle of permissions.

    ``tenant_id`` NULL means a system role shipped with the platform; institutions may
    clone or author their own roles without any code change.
    """

    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_roles_tenant_code"),
        Index("ix_roles_rank", "rank"),
    )

    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), default=None, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    #: Lower rank == more senior. Used to prevent privilege escalation on delegation.
    rank: Mapped[int] = mapped_column(default=50, nullable=False)
    #: The org level at which this role is normally granted.
    scope_kind: Mapped[str] = mapped_column(String(32), default="department", nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_trainee_role: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_supervisor_role: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Denormalised permission code list — the authoritative store, kept as JSON so a
    #: role can be edited atomically and versioned in the audit log.
    permission_codes: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    assignments: Mapped[list[RoleAssignment]] = relationship(
        back_populates="role", cascade="all, delete-orphan", passive_deletes=True
    )

    def grants(self, permission_code: str) -> bool:
        return "*" in self.permission_codes or permission_code in self.permission_codes


class RoleAssignment(Base, IdMixin, TimestampMixin, SyncMixin):
    """Binds a user to a role *within an organisational scope*.

    A consultant may hold ``consultant`` in Surgery and ``head_of_department`` in
    Plastic Surgery simultaneously; permissions resolve to the union, evaluated against
    the org subtree of each assignment.
    """

    __tablename__ = "role_assignments"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", "org_unit_id", name="uq_role_assignment"),
        Index("ix_role_assignments_org", "org_unit_id"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[str] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: NULL == platform-wide (national) scope.
    org_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="CASCADE"), default=None
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    starts_on: Mapped[date | None] = mapped_column(Date, default=None)
    ends_on: Mapped[date | None] = mapped_column(Date, default=None)
    granted_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    user: Mapped[User] = relationship(back_populates="role_assignments", foreign_keys=[user_id])
    role: Mapped[Role] = relationship(back_populates="assignments")
    org_unit: Mapped[OrgUnit | None] = relationship()

    def is_current(self, on: date) -> bool:
        if self.starts_on and on < self.starts_on:
            return False
        if self.ends_on and on > self.ends_on:
            return False
        return True


class SupervisorProfile(Base, IdMixin, TimestampMixin):
    """Capacity and expertise data used by the automatic supervisor allocator."""

    __tablename__ = "supervisor_profiles"
    __table_args__ = (UniqueConstraint("user_id", name="uq_supervisor_profile_user"),)

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Free-text expertise tags matched against project keywords.
    expertise: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    #: Sub-specialty interests, methodology strengths (e.g. "biostatistics").
    methodologies: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    max_supervisees: Mapped[int] = mapped_column(default=5, nullable=False)
    max_clinical_trainees: Mapped[int] = mapped_column(default=8, nullable=False)
    accepting_new: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    unavailable_from: Mapped[date | None] = mapped_column(Date, default=None)
    unavailable_to: Mapped[date | None] = mapped_column(Date, default=None)
    completed_supervisions: Mapped[int] = mapped_column(default=0, nullable=False)
    #: User ids the supervisor must not be paired with (declared conflicts of interest).
    conflicts_of_interest: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    user: Mapped[User] = relationship()

    def is_available_on(self, on: date) -> bool:
        if not self.accepting_new:
            return False
        if self.unavailable_from and self.unavailable_to:
            return not (self.unavailable_from <= on <= self.unavailable_to)
        return True


class UserSession(Base, IdMixin, TimestampMixin):
    """Refresh-token session, so sessions can be listed and revoked per device."""

    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_sessions_user_active", "user_id", "revoked_at"),)

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    jti: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    device_label: Mapped[str | None] = mapped_column(String(160), default=None)
    ip_address: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
