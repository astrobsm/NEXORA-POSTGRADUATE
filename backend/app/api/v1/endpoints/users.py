"""User directory, roles, role assignment and supervisor profiles."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, TenantId
from app.db.base import owned_or_shared
from app.core.rbac import PERMISSIONS, TRAINEE_ROLE_CODES
from app.core.security import (
    PasswordPolicyError,
    enforce_password_policy,
    hash_password,
)
from app.models.enums import AuditAction, UserStatus
from app.models.identity import Role, RoleAssignment, SupervisorProfile, User
from app.models.tenancy import OrgUnit
from app.schemas.common import Page
from app.schemas.identity import (
    RoleAssignRequest,
    RoleCreate,
    RoleOut,
    SupervisorProfileOut,
    SupervisorProfileUpsert,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.services import audit

router = APIRouter()


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
@router.get("", response_model=Page[UserOut], summary="User directory")
def list_users(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    search: str | None = None,
    role_code: str | None = None,
    org_unit_id: str | None = None,
    user_status: str | None = Query(default=None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    principal.require("identity.user.read")
    stmt = select(User).where(User.tenant_id == tenant_id, User.deleted_at.is_(None))

    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            User.first_name.ilike(like)
            | User.last_name.ilike(like)
            | User.email.ilike(like)
            | User.registration_number.ilike(like)
            | User.staff_number.ilike(like)
        )
    if user_status:
        stmt = stmt.where(User.status == user_status)
    if role_code or org_unit_id:
        stmt = stmt.join(RoleAssignment, RoleAssignment.user_id == User.id)
        if role_code:
            stmt = stmt.join(Role, Role.id == RoleAssignment.role_id).where(Role.code == role_code)
        if org_unit_id:
            stmt = stmt.where(RoleAssignment.org_unit_id == org_unit_id)
        stmt = stmt.distinct()

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(User.last_name, User.first_name)
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return Page[UserOut](
        items=[UserOut.model_validate(u) for u in rows], total=total, page=page, page_size=page_size
    )


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED,
             summary="Create a user")
def create_user(
    payload: UserCreate,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    principal.require("identity.user.manage")

    email = payload.email.lower()
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An account already exists for {email}.",
        )

    hashed = None
    if payload.password:
        try:
            enforce_password_policy(payload.password)
        except PasswordPolicyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        hashed = hash_password(payload.password)

    user = User(
        tenant_id=tenant_id,
        email=email,
        first_name=payload.first_name,
        middle_name=payload.middle_name,
        last_name=payload.last_name,
        title=payload.title,
        phone=payload.phone,
        discipline=payload.discipline,
        registration_number=payload.registration_number,
        staff_number=payload.staff_number,
        hashed_password=hashed,
        status=UserStatus.ACTIVE if hashed else UserStatus.INVITED,
        must_change_password=bool(hashed),
    )
    db.add(user)
    db.flush()

    if payload.role_code:
        role = db.execute(
            select(Role).where(
                Role.code == payload.role_code,
                owned_or_shared(Role.tenant_id, tenant_id),
            ).order_by(Role.tenant_id.is_(None))
        ).scalars().first()
        if role is None:
            raise HTTPException(status_code=404, detail=f"Role '{payload.role_code}' not found.")
        _guard_escalation(db, principal, role)
        db.add(
            RoleAssignment(
                user_id=user.id, role_id=role.id, org_unit_id=payload.org_unit_id,
                is_primary=True, granted_by_id=principal.id,
            )
        )

    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="user", entity_id=user.id,
                 summary=f"Created account for {user.full_name} ({email})", **meta)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.get("/{user_id}", response_model=UserOut, summary="Read a user")
def get_user(user_id: str, db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    if user_id != principal.id:
        principal.require("identity.user.read")
    user = db.get(User, user_id)
    if user is None or user.tenant_id != tenant_id or user.deleted_at is not None:
        raise HTTPException(status_code=404, detail="User not found.")
    return UserOut.model_validate(user)


@router.patch("/{user_id}", response_model=UserOut, summary="Update a user")
def update_user(
    user_id: str,
    payload: UserUpdate,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    user = db.get(User, user_id)
    if user is None or user.tenant_id != tenant_id or user.deleted_at is not None:
        raise HTTPException(status_code=404, detail="User not found.")

    data = payload.model_dump(exclude_unset=True)
    if user_id != principal.id:
        principal.require("identity.user.manage")
    elif "status" in data:
        raise HTTPException(status_code=403, detail="You cannot change your own account status.")

    before = user.as_dict()
    for field, value in data.items():
        setattr(user, field, value)
    db.add(user)
    audit.record(db, action=AuditAction.UPDATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="user", entity_id=user.id, summary=f"Updated {user.full_name}",
                 changes=audit.diff(before, user.as_dict()), **meta)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.get("/{user_id}/roles", summary="A user's role assignments")
def user_roles(user_id: str, db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    if user_id != principal.id:
        principal.require("identity.user.read")
    rows = db.execute(
        select(RoleAssignment).where(RoleAssignment.user_id == user_id)
    ).scalars().all()
    out = []
    for row in rows:
        unit = db.get(OrgUnit, row.org_unit_id) if row.org_unit_id else None
        out.append(
            {
                "id": row.id,
                "role_id": row.role_id,
                "role_code": row.role.code if row.role else None,
                "role_name": row.role.name if row.role else None,
                "org_unit_id": row.org_unit_id,
                "org_unit_name": unit.name if unit else None,
                "is_primary": row.is_primary,
                "starts_on": row.starts_on,
                "ends_on": row.ends_on,
            }
        )
    return out


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------
def _guard_escalation(db, principal, role: Role) -> None:
    """Nobody may grant a role more senior than their own most senior role.

    Without this, a department coordinator could appoint themselves CMD. Platform
    administrators are exempt.
    """
    if principal.is_superuser:
        return
    own_ranks = [
        r.rank
        for r in db.execute(
            select(Role)
            .join(RoleAssignment, RoleAssignment.role_id == Role.id)
            .where(RoleAssignment.user_id == principal.id)
        ).scalars()
    ]
    if not own_ranks or role.rank < min(own_ranks):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot grant '{role.name}' — it is more senior than your own role.",
        )


@router.get("/roles/catalogue", response_model=list[RoleOut], summary="Available roles")
def list_roles(db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    principal.require("identity.user.read")
    rows = db.execute(
        select(Role)
        .where(owned_or_shared(Role.tenant_id, tenant_id), Role.deleted_at.is_(None))
        .order_by(Role.rank, Role.name)
    ).scalars().all()
    return [RoleOut.model_validate(r) for r in rows]


@router.get("/roles/permissions", summary="The permission vocabulary")
def list_permissions(principal: CurrentPrincipal):
    """Every permission code the platform understands, grouped for the role editor."""
    principal.require("identity.user.read")
    grouped: dict[str, list[dict]] = {}
    for p in PERMISSIONS:
        grouped.setdefault(p.category, []).append({"code": p.code, "name": p.name})
    return grouped


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED,
             summary="Create an institution-specific role")
def create_role(
    payload: RoleCreate,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    """Institutions define their own roles. A new role may not carry permissions the
    creator does not themselves hold."""
    principal.require("identity.role.manage")

    if not principal.is_superuser:
        missing = [c for c in payload.permission_codes if not principal.has(c)]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You cannot grant permissions you do not hold: " + ", ".join(missing[:5]),
            )

    existing = db.execute(
        select(Role).where(Role.tenant_id == tenant_id, Role.code == payload.code)
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Role code '{payload.code}' already exists.")

    role = Role(
        tenant_id=tenant_id,
        code=payload.code,
        name=payload.name,
        description=payload.description,
        rank=payload.rank,
        scope_kind=payload.scope_kind,
        permission_codes=payload.permission_codes,
        is_trainee_role=payload.is_trainee_role,
        is_supervisor_role=payload.is_supervisor_role,
        is_system=False,
    )
    db.add(role)
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="role", entity_id=role.id, summary=f"Created role '{payload.name}'", **meta)
    db.commit()
    db.refresh(role)
    return RoleOut.model_validate(role)


@router.post("/roles/assign", summary="Assign a role within a scope")
def assign_role(
    payload: RoleAssignRequest,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    principal.require("identity.assignment.manage")

    user = db.get(User, payload.user_id)
    if user is None or user.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="User not found.")
    role = db.get(Role, payload.role_id)
    if role is None or role.tenant_id not in (tenant_id, None):
        raise HTTPException(status_code=404, detail="Role not found.")
    _guard_escalation(db, principal, role)

    if payload.org_unit_id:
        unit = db.get(OrgUnit, payload.org_unit_id)
        if unit is None or unit.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Organisational unit not found.")

    existing = db.execute(
        select(RoleAssignment).where(
            RoleAssignment.user_id == payload.user_id,
            RoleAssignment.role_id == payload.role_id,
            RoleAssignment.org_unit_id == payload.org_unit_id,
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{user.full_name} already holds '{role.name}' at this scope.",
        )

    assignment = RoleAssignment(
        user_id=payload.user_id,
        role_id=payload.role_id,
        org_unit_id=payload.org_unit_id,
        is_primary=payload.is_primary,
        starts_on=payload.starts_on,
        ends_on=payload.ends_on,
        notes=payload.notes,
        granted_by_id=principal.id,
    )
    db.add(assignment)
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="role_assignment", entity_id=assignment.id,
                 summary=f"Granted '{role.name}' to {user.full_name}", **meta)
    db.commit()
    return {"detail": f"'{role.name}' granted to {user.full_name}.", "id": assignment.id}


@router.delete("/roles/assignments/{assignment_id}", summary="Revoke a role assignment")
def revoke_role(assignment_id: str, db: DbSession, principal: CurrentPrincipal,
                tenant_id: TenantId, meta: ClientMeta):
    principal.require("identity.assignment.manage")
    assignment = db.get(RoleAssignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Role assignment not found.")
    role = assignment.role
    _guard_escalation(db, principal, role)
    db.delete(assignment)
    audit.record(db, action=AuditAction.DELETE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="role_assignment", entity_id=assignment_id,
                 summary=f"Revoked '{role.name if role else assignment.role_id}'", **meta)
    db.commit()
    return {"detail": "Role assignment revoked."}


# --------------------------------------------------------------------------
# Supervisor profiles
# --------------------------------------------------------------------------
@router.get("/{user_id}/supervisor-profile", response_model=SupervisorProfileOut | None,
            summary="Supervision capacity and expertise")
def get_supervisor_profile(user_id: str, db: DbSession, principal: CurrentPrincipal,
                           tenant_id: TenantId):
    profile = db.execute(
        select(SupervisorProfile).where(SupervisorProfile.user_id == user_id)
    ).scalar_one_or_none()
    return SupervisorProfileOut.model_validate(profile) if profile else None


@router.put("/{user_id}/supervisor-profile", response_model=SupervisorProfileOut,
            summary="Create or update a supervisor profile")
def upsert_supervisor_profile(
    user_id: str,
    payload: SupervisorProfileUpsert,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    if user_id != principal.id:
        principal.require("identity.user.manage")
    user = db.get(User, user_id)
    if user is None or user.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="User not found.")

    profile = db.execute(
        select(SupervisorProfile).where(SupervisorProfile.user_id == user_id)
    ).scalar_one_or_none()
    if profile is None:
        profile = SupervisorProfile(user_id=user_id, tenant_id=tenant_id)
        db.add(profile)

    for field, value in payload.model_dump().items():
        setattr(profile, field, value)
    audit.record(db, action=AuditAction.UPDATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="supervisor_profile", entity_id=profile.id,
                 summary=f"Updated supervision profile for {user.full_name}", **meta)
    db.commit()
    db.refresh(profile)
    return SupervisorProfileOut.model_validate(profile)


@router.get("/trainees/directory", response_model=Page[UserOut], summary="Trainee directory")
def trainee_directory(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    principal.require("identity.user.read")
    stmt = (
        select(User)
        .join(RoleAssignment, RoleAssignment.user_id == User.id)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(
            User.tenant_id == tenant_id,
            User.deleted_at.is_(None),
            Role.code.in_(tuple(TRAINEE_ROLE_CODES)),
        )
        .distinct()
    )
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(User.last_name).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return Page[UserOut](
        items=[UserOut.model_validate(u) for u in rows], total=total, page=page, page_size=page_size
    )
