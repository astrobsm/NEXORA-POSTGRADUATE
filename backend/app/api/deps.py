"""FastAPI dependencies: authentication, the request principal, and scoped RBAC."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.rbac import SUPERUSER_WILDCARD
from app.core.security import TokenError, decode_token
from app.db.session import get_db
from app.models.identity import Role, RoleAssignment, User
from app.models.tenancy import OrgUnit

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.api_v1_prefix}/auth/login", auto_error=False
)


@dataclass(slots=True)
class Principal:
    """The authenticated caller, with permissions resolved per organisational scope."""

    user: User
    #: permission code -> set of org unit ids at which it is held.
    #: ``None`` in the set means platform-wide (no org restriction).
    grants: dict[str, set[str | None]] = field(default_factory=dict)
    role_codes: set[str] = field(default_factory=set)
    #: Every org unit id the caller can see, expanded through subtrees.
    visible_org_unit_ids: set[str] = field(default_factory=set)
    is_superuser: bool = False

    @property
    def id(self) -> str:
        return self.user.id

    @property
    def tenant_id(self) -> str | None:
        return self.user.tenant_id

    def has(self, permission: str, *, org_unit_id: str | None = None) -> bool:
        if self.is_superuser:
            return True
        scopes = self.grants.get(permission)
        if not scopes:
            return False
        if org_unit_id is None:
            return True
        if None in scopes:
            return True
        return org_unit_id in scopes

    def require(self, permission: str, *, org_unit_id: str | None = None) -> None:
        if not self.has(permission, org_unit_id=org_unit_id):
            where = " at the requested scope" if org_unit_id else ""
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission}' is required{where}.",
            )

    def has_any(self, *permissions: str) -> bool:
        return any(self.has(p) for p in permissions)


# --------------------------------------------------------------------------
def _expand_subtree(db: Session, org_unit_ids: set[str]) -> set[str]:
    """A permission held at a node also applies to everything beneath it."""
    if not org_unit_ids:
        return set()
    units = db.execute(select(OrgUnit).where(OrgUnit.id.in_(org_unit_ids))).scalars().all()
    expanded = set(org_unit_ids)
    for unit in units:
        if not unit.path:
            continue
        descendants = db.execute(
            select(OrgUnit.id).where(
                OrgUnit.tenant_id == unit.tenant_id,
                OrgUnit.path.like(f"{unit.path}/%"),
            )
        ).scalars().all()
        expanded.update(descendants)
    return expanded


def build_principal(db: Session, user: User) -> Principal:
    """Resolve the caller's effective permissions across all their role assignments."""
    principal = Principal(user=user, is_superuser=user.is_platform_admin)

    assignments = db.execute(
        select(RoleAssignment, Role)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(RoleAssignment.user_id == user.id)
    ).all()

    today = date.today()
    direct_scopes: set[str] = set()

    for assignment, role in assignments:
        if not assignment.is_current(today):
            continue
        principal.role_codes.add(role.code)
        scope = assignment.org_unit_id
        if scope:
            direct_scopes.add(scope)

        for code in role.permission_codes or []:
            if code == SUPERUSER_WILDCARD:
                principal.is_superuser = True
                continue
            principal.grants.setdefault(code, set()).add(scope)

    principal.visible_org_unit_ids = _expand_subtree(db, direct_scopes)

    # A permission granted at a node is honoured throughout its subtree.
    for code, scopes in principal.grants.items():
        expanded = set(scopes)
        expanded |= _expand_subtree(db, {s for s in scopes if s is not None})
        principal.grants[code] = expanded

    return principal


# --------------------------------------------------------------------------
async def get_current_principal(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    token: Annotated[str | None, Depends(oauth2_scheme)] = None,
) -> Principal:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(token, expected_type="access")
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = db.get(User, payload["sub"])
    if user is None or user.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found.")
    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Account is {user.status}; contact your training administrator.",
        )

    principal = build_principal(db, user)
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
DbSession = Annotated[Session, Depends(get_db)]


def require_permission(permission: str) -> Callable[[Principal], Principal]:
    """Route dependency factory: ``Depends(require_permission("logbook.entry.validate"))``."""

    def _dependency(principal: CurrentPrincipal) -> Principal:
        principal.require(permission)
        return principal

    return _dependency


def tenant_scope(principal: CurrentPrincipal) -> str:
    """The tenant every query in this request must be filtered by."""
    if principal.tenant_id:
        return principal.tenant_id
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            "This account is not bound to an institution. Platform administrators must "
            "supply X-Tenant-Id to operate on institutional data."
        ),
    )


def resolve_tenant(
    principal: CurrentPrincipal,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
) -> str:
    """Tenant resolution that lets a platform administrator act on a chosen institution."""
    if principal.is_superuser and x_tenant_id:
        return x_tenant_id
    if principal.tenant_id:
        return principal.tenant_id
    if x_tenant_id:
        return x_tenant_id
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="No institution context. Supply the X-Tenant-Id header.",
    )


TenantId = Annotated[str, Depends(resolve_tenant)]


def client_meta(request: Request) -> dict[str, str | None]:
    """IP, user-agent and request id for the audit trail."""
    return {
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "request_id": request.headers.get("x-request-id"),
    }


ClientMeta = Annotated[dict, Depends(client_meta)]
