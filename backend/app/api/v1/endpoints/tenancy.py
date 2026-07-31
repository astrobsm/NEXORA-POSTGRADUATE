"""Institutions, the organisational hierarchy, and institution branding."""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, TenantId
from app.models.branding import (
    ALLOWED_CONTENT_TYPES,
    MAX_ASSET_BYTES,
    BrandingAsset,
    BrandingAssetKind,
)
from app.models.enums import AuditAction, OrgKind
from app.models.tenancy import OrgUnit, Tenant
from app.schemas.training import OrgUnitCreate, OrgUnitOut, OrgUnitTree, TenantOut
from app.services import audit
from app.services.branding import BrandingAssetError, dimensions_of, validate

router = APIRouter()

#: Which kinds may nest inside which. Enforced so an institution cannot accidentally
#: build a hierarchy that later breaks subtree permission resolution.
ALLOWED_CHILDREN: dict[str, set[str]] = {
    OrgKind.NATIONAL: {OrgKind.COLLEGE, OrgKind.HOSPITAL},
    OrgKind.COLLEGE: {OrgKind.HOSPITAL, OrgKind.FACULTY},
    OrgKind.HOSPITAL: {OrgKind.FACULTY, OrgKind.DEPARTMENT},
    OrgKind.FACULTY: {OrgKind.DEPARTMENT},
    OrgKind.DEPARTMENT: {OrgKind.UNIT, OrgKind.SUBSPECIALTY, OrgKind.PROGRAMME},
    OrgKind.UNIT: {OrgKind.SUBSPECIALTY, OrgKind.PROGRAMME},
    OrgKind.SUBSPECIALTY: {OrgKind.PROGRAMME},
    OrgKind.PROGRAMME: set(),
}


@router.get("/tenants/current", response_model=TenantOut, summary="The signed-in institution")
def current_tenant(db: DbSession, tenant_id: TenantId):
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Institution not found.")
    return TenantOut.model_validate(tenant)


@router.get("/tenants", response_model=list[TenantOut], summary="List institutions")
def list_tenants(db: DbSession, principal: CurrentPrincipal):
    principal.require("platform.tenant.manage")
    rows = db.execute(
        select(Tenant).where(Tenant.deleted_at.is_(None)).order_by(Tenant.name)
    ).scalars().all()
    return [TenantOut.model_validate(t) for t in rows]


@router.patch("/tenants/current", response_model=TenantOut, summary="Update institution settings")
def update_tenant(
    payload: dict,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    """Update institutional configuration — branding, timezone, accrediting bodies, and
    the free-form ``settings`` map that drives configurable behaviour."""
    principal.require("tenancy.settings.manage")
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Institution not found.")

    before = tenant.as_dict()
    editable = {
        "name", "state", "city", "address", "timezone", "locale", "contact_email",
        "contact_phone", "website", "logo_key", "accrediting_bodies", "branding", "settings",
    }
    for key, value in payload.items():
        if key in editable:
            setattr(tenant, key, value)
    db.add(tenant)
    audit.record(db, action=AuditAction.CONFIG_CHANGE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="tenant", entity_id=tenant.id, summary="Updated institution settings",
                 changes=audit.diff(before, tenant.as_dict()), **meta)
    db.commit()
    db.refresh(tenant)
    return TenantOut.model_validate(tenant)


# --------------------------------------------------------------------------
# Branding assets
# --------------------------------------------------------------------------
def _asset_or_404(db, tenant_id: str, kind: str) -> BrandingAsset:
    asset = db.execute(
        select(BrandingAsset).where(
            BrandingAsset.tenant_id == tenant_id, BrandingAsset.kind == kind
        )
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {kind.replace('_', ' ')} has been uploaded for this institution.",
        )
    return asset


def _validate_kind(kind: str) -> str:
    valid = {k.value for k in BrandingAssetKind}
    if kind not in valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown branding asset '{kind}'. Valid: {', '.join(sorted(valid))}.",
        )
    return kind


@router.get("/public/branding", summary="Branding for the sign-in screen")
def public_branding(db: DbSession, code: str | None = None):
    """Institution name, colours and asset URLs, without a session.

    The sign-in screen has to render the hospital's identity before anyone has
    authenticated, so this is deliberately public. It exposes only what would be
    printed on the building: name, code, colours and logo URLs.

    Resolution order: an explicit ``code``, else the single tenant on a
    single-institution deployment. A multi-tenant deployment without a code gets
    the platform default, because guessing would be worse.
    """
    tenant = None
    if code:
        tenant = db.execute(
            select(Tenant).where(Tenant.code == code, Tenant.deleted_at.is_(None))
        ).scalar_one_or_none()
    else:
        tenants = db.execute(
            select(Tenant).where(Tenant.deleted_at.is_(None), Tenant.is_active.is_(True)).limit(2)
        ).scalars().all()
        if len(tenants) == 1:
            tenant = tenants[0]

    if tenant is None:
        return {
            "tenant_id": None,
            "name": "Residency Training Console",
            "code": None,
            "colours": {},
            "assets": {},
        }

    kinds = db.execute(
        select(BrandingAsset.kind).where(BrandingAsset.tenant_id == tenant.id)
    ).scalars().all()
    base = f"/api/v1/tenancy/tenants/{tenant.id}/branding"

    return {
        "tenant_id": tenant.id,
        "name": tenant.name,
        "code": tenant.code,
        "colours": tenant.branding or {},
        "manifest_url": f"/api/v1/tenancy/tenants/{tenant.id}/manifest.webmanifest",
        "assets": {kind: f"{base}/{kind}" for kind in kinds},
    }


@router.get("/tenants/current/branding", summary="Branding assets and colours")
def branding_summary(db: DbSession, tenant_id: TenantId):
    """What this institution has configured. Unauthenticated callers cannot reach
    this, but it deliberately carries no sensitive data — the sign-in screen needs
    the logo before anyone has a session."""
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Institution not found.")

    assets = db.execute(
        select(BrandingAsset).where(BrandingAsset.tenant_id == tenant_id)
    ).scalars().all()

    return {
        "tenant_id": tenant.id,
        "name": tenant.name,
        "code": tenant.code,
        "colours": tenant.branding or {},
        "max_bytes": MAX_ASSET_BYTES,
        "accepted_types": sorted(ALLOWED_CONTENT_TYPES),
        "assets": {
            asset.kind: {
                "url": f"/api/v1/tenancy/tenants/{tenant.id}/branding/{asset.kind}",
                "content_type": asset.content_type,
                "filename": asset.filename,
                "size_bytes": asset.size_bytes,
                "width": asset.width,
                "height": asset.height,
                "checksum": asset.checksum,
                "updated_at": asset.updated_at,
            }
            for asset in assets
        },
    }


@router.put("/tenants/current/branding/{kind}", status_code=status.HTTP_201_CREATED,
            summary="Upload a branding asset")
async def upload_branding_asset(
    kind: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
    file: UploadFile = File(...),
):
    """Upload the institution's logo, app icon, favicon or sign-in backdrop.

    The declared content type is not trusted — the real format is confirmed from
    the file's magic bytes, and SVG is rejected if it carries scripting.
    """
    principal.require("tenancy.settings.manage")
    _validate_kind(kind)

    data = await file.read()
    try:
        content_type, checksum = validate(data, file.content_type, kind=kind)
    except BrandingAssetError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    width, height = dimensions_of(data, content_type)
    if kind == BrandingAssetKind.ICON and width and height and width != height:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"The app icon must be square; this one is {width}×{height}. "
                "Installed apps crop to a square, so a wide logo loses its edges."
            ),
        )

    asset = db.execute(
        select(BrandingAsset).where(
            BrandingAsset.tenant_id == tenant_id, BrandingAsset.kind == kind
        )
    ).scalar_one_or_none()
    replacing = asset is not None

    if asset is None:
        asset = BrandingAsset(tenant_id=tenant_id, kind=kind)
        db.add(asset)

    asset.content_type = content_type
    asset.filename = (file.filename or "")[:255] or None
    asset.size_bytes = len(data)
    asset.checksum = checksum
    asset.data = data
    asset.width = width
    asset.height = height
    asset.uploaded_by_id = principal.id

    audit.record(db, action=AuditAction.CONFIG_CHANGE, tenant_id=tenant_id,
                 actor_id=principal.id, entity_type="branding_asset", entity_id=asset.id,
                 summary=f"{'Replaced' if replacing else 'Uploaded'} institution {kind}"
                         f" ({content_type}, {len(data) // 1024} KiB)", **meta)
    db.commit()
    db.refresh(asset)

    return {
        "kind": asset.kind,
        "url": f"/api/v1/tenancy/tenants/{tenant_id}/branding/{asset.kind}",
        "content_type": asset.content_type,
        "size_bytes": asset.size_bytes,
        "width": asset.width,
        "height": asset.height,
        "checksum": asset.checksum,
    }


@router.get("/tenants/{tenant_id}/branding/{kind}", summary="Serve a branding asset")
def serve_branding_asset(tenant_id: str, kind: str, request: Request, db: DbSession):
    """Serve the asset bytes.

    Deliberately unauthenticated: the sign-in screen, the browser tab icon and the
    PWA manifest all need it before a session exists, and a logo is public by
    nature. It is served with a sandbox CSP and `nosniff` so that an uploaded SVG
    cannot execute even if opened directly.
    """
    _validate_kind(kind)
    asset = _asset_or_404(db, tenant_id, kind)

    # An unchanged crest should not be re-sent on every page load.
    if request.headers.get("if-none-match") == asset.etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": asset.etag})

    return Response(
        content=asset.data,
        media_type=asset.content_type,
        headers={
            "ETag": asset.etag,
            "Cache-Control": "public, max-age=300, must-revalidate",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "Content-Disposition": f'inline; filename="{kind}"',
        },
    )


@router.delete("/tenants/current/branding/{kind}", summary="Remove a branding asset")
def delete_branding_asset(kind: str, db: DbSession, principal: CurrentPrincipal,
                          tenant_id: TenantId, meta: ClientMeta):
    principal.require("tenancy.settings.manage")
    _validate_kind(kind)
    asset = _asset_or_404(db, tenant_id, kind)
    db.delete(asset)
    audit.record(db, action=AuditAction.CONFIG_CHANGE, tenant_id=tenant_id,
                 actor_id=principal.id, entity_type="branding_asset", entity_id=asset.id,
                 summary=f"Removed institution {kind}", **meta)
    db.commit()
    return {"detail": f"The institution {kind.replace('_', ' ')} has been removed."}


@router.get("/tenants/{tenant_id}/manifest.webmanifest",
            summary="Institution-branded PWA manifest")
def tenant_manifest(tenant_id: str, db: DbSession):
    """A manifest carrying the institution's own name, colours and icon, so an
    installed app appears on the home screen as the hospital's, not ours."""
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Institution not found.")

    branding = tenant.branding or {}
    base = f"/api/v1/tenancy/tenants/{tenant_id}/branding"
    has_icon = db.execute(
        select(BrandingAsset.id).where(
            BrandingAsset.tenant_id == tenant_id, BrandingAsset.kind == BrandingAssetKind.ICON
        )
    ).scalar_one_or_none()

    icons = (
        [{"src": f"{base}/icon", "sizes": "any", "purpose": "any"}]
        if has_icon
        else [
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        ]
    )

    return JSONResponse(
        content={
            "name": f"{tenant.name} — Residency Training Console",
            "short_name": branding.get("logo_text") or tenant.code,
            "id": f"/?tenant={tenant.code}",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": branding.get("background", "#f6f8f5"),
            "theme_color": branding.get("primary", "#166534"),
            "lang": tenant.locale or "en-NG",
            "categories": ["medical", "education", "productivity"],
            "icons": icons,
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=600"},
    )


# --------------------------------------------------------------------------
# Organisational units
# --------------------------------------------------------------------------
@router.get("/org-units", response_model=list[OrgUnitOut], summary="List organisational units")
def list_org_units(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    kind: str | None = None,
    parent_id: str | None = None,
    include_inactive: bool = False,
):
    principal.require("tenancy.orgunit.read")
    stmt = select(OrgUnit).where(OrgUnit.tenant_id == tenant_id, OrgUnit.deleted_at.is_(None))
    if kind:
        stmt = stmt.where(OrgUnit.kind == kind)
    if parent_id:
        stmt = stmt.where(OrgUnit.parent_id == parent_id)
    if not include_inactive:
        stmt = stmt.where(OrgUnit.is_active.is_(True))
    rows = db.execute(stmt.order_by(OrgUnit.depth, OrgUnit.sort_order, OrgUnit.name)).scalars().all()
    return [OrgUnitOut.model_validate(u) for u in rows]


@router.get("/org-units/tree", response_model=list[OrgUnitTree], summary="Full org hierarchy")
def org_tree(db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId,
             root_id: str | None = None):
    """The whole ladder in one call — the shape the navigation sidebar renders."""
    principal.require("tenancy.orgunit.read")
    rows = db.execute(
        select(OrgUnit)
        .where(OrgUnit.tenant_id == tenant_id, OrgUnit.deleted_at.is_(None))
        .order_by(OrgUnit.depth, OrgUnit.sort_order, OrgUnit.name)
    ).scalars().all()

    nodes: dict[str, OrgUnitTree] = {
        u.id: OrgUnitTree.model_validate(u, from_attributes=True) for u in rows
    }
    for node in nodes.values():
        node.children = []

    roots: list[OrgUnitTree] = []
    for unit in rows:
        node = nodes[unit.id]
        if unit.parent_id and unit.parent_id in nodes:
            nodes[unit.parent_id].children.append(node)
        else:
            roots.append(node)

    if root_id:
        return [nodes[root_id]] if root_id in nodes else []
    return roots


@router.post("/org-units", response_model=OrgUnitOut, status_code=status.HTTP_201_CREATED,
             summary="Create an organisational unit")
def create_org_unit(
    payload: OrgUnitCreate,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    principal.require("tenancy.orgunit.manage")

    parent = None
    if payload.parent_id:
        parent = db.get(OrgUnit, payload.parent_id)
        if parent is None or parent.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Parent unit not found.")
        allowed = ALLOWED_CHILDREN.get(parent.kind, set())
        if payload.kind not in allowed:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"A '{payload.kind}' cannot sit directly under a '{parent.kind}'. "
                    f"Permitted children of a {parent.kind}: "
                    f"{', '.join(sorted(allowed)) or 'none'}."
                ),
            )

    existing = db.execute(
        select(OrgUnit).where(OrgUnit.tenant_id == tenant_id, OrgUnit.code == payload.code)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Code '{payload.code}' is already used by '{existing.name}'.",
        )

    unit = OrgUnit(
        tenant_id=tenant_id,
        parent_id=payload.parent_id,
        kind=payload.kind,
        name=payload.name,
        short_name=payload.short_name,
        code=payload.code,
        discipline=payload.discipline,
        specialty_id=payload.specialty_id,
        head_user_id=payload.head_user_id,
        capacity=payload.capacity,
        description=payload.description,
        depth=(parent.depth + 1) if parent else 0,
    )
    unit.parent = parent
    unit.path = unit.compute_path()
    db.add(unit)
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="org_unit", entity_id=unit.id,
                 summary=f"Created {payload.kind} '{payload.name}'", **meta)
    db.commit()
    db.refresh(unit)
    return OrgUnitOut.model_validate(unit)


@router.get("/org-units/{unit_id}", response_model=OrgUnitOut, summary="Read one unit")
def get_org_unit(unit_id: str, db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId):
    principal.require("tenancy.orgunit.read")
    unit = db.get(OrgUnit, unit_id)
    if unit is None or unit.tenant_id != tenant_id or unit.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Organisational unit not found.")
    return OrgUnitOut.model_validate(unit)


@router.patch("/org-units/{unit_id}", response_model=OrgUnitOut, summary="Update a unit")
def update_org_unit(
    unit_id: str,
    payload: dict,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
):
    principal.require("tenancy.orgunit.manage")
    unit = db.get(OrgUnit, unit_id)
    if unit is None or unit.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Organisational unit not found.")

    before = unit.as_dict()
    editable = {
        "name", "short_name", "discipline", "specialty_id", "head_user_id",
        "capacity", "settings", "description", "sort_order", "is_active",
    }
    for key, value in payload.items():
        if key in editable:
            setattr(unit, key, value)
    db.add(unit)
    audit.record(db, action=AuditAction.UPDATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="org_unit", entity_id=unit.id, summary=f"Updated unit '{unit.name}'",
                 changes=audit.diff(before, unit.as_dict()), **meta)
    db.commit()
    db.refresh(unit)
    return OrgUnitOut.model_validate(unit)
