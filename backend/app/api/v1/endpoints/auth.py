"""Authentication: login, MFA, refresh, logout, self-service password and profile."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, build_principal
from app.core.config import settings
from app.core.security import (
    PasswordPolicyError,
    TokenError,
    check_recovery_code,
    create_token,
    decode_token,
    enforce_password_policy,
    generate_mfa_secret,
    generate_recovery_codes,
    hash_password,
    hash_recovery_code,
    mfa_provisioning_uri,
    needs_rehash,
    verify_mfa_code,
    verify_password,
)
from app.db.base import utcnow
from app.models.enums import AuditAction
from app.models.identity import RoleAssignment, User, UserSession
from app.models.tenancy import OrgUnit, Tenant
from app.models.training import Enrolment
from app.schemas.identity import (
    LoginRequest,
    MeResponse,
    MfaChallenge,
    MfaEnrolResponse,
    MfaVerifyRequest,
    PasswordChangeRequest,
    RefreshRequest,
    RoleAssignmentOut,
    TokenPair,
    UserOut,
)
from app.services import audit

router = APIRouter()


# --------------------------------------------------------------------------
def _issue_tokens(db, user: User, *, device_id: str | None, device_label: str | None,
                  meta: dict) -> TokenPair:
    access = create_token(user.id, "access", claims={"tenant": user.tenant_id})
    refresh = create_token(user.id, "refresh", claims={"tenant": user.tenant_id})
    payload = decode_token(refresh, expected_type="refresh")

    db.add(
        UserSession(
            user_id=user.id,
            jti=payload["jti"],
            device_label=device_label or device_id,
            ip_address=meta.get("ip_address"),
            user_agent=meta.get("user_agent"),
            expires_at=utcnow() + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    user.last_login_at = utcnow()
    user.failed_login_count = 0
    user.locked_until = None
    db.add(user)
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


def _register_failure(db, user: User | None, email: str, meta: dict) -> None:
    if user is not None:
        user.failed_login_count += 1
        if user.failed_login_count >= settings.max_failed_logins:
            user.locked_until = utcnow() + timedelta(minutes=settings.lockout_minutes)
        db.add(user)
    audit.record(
        db,
        action=AuditAction.LOGIN_FAILED,
        tenant_id=user.tenant_id if user else None,
        actor_id=user.id if user else None,
        actor_label=email,
        entity_type="user",
        entity_id=user.id if user else None,
        summary=f"Failed sign-in attempt for {email}",
        succeeded=False,
        **meta,
    )
    db.commit()


# --------------------------------------------------------------------------
@router.post("/login", response_model=TokenPair | MfaChallenge, summary="Sign in")
def login(payload: LoginRequest, db: DbSession, meta: ClientMeta):
    """Password sign-in. Returns an MFA challenge when the account has MFA enabled.

    The response is deliberately identical for unknown accounts and wrong passwords, so
    the endpoint cannot be used to enumerate staff email addresses.
    """
    user = db.execute(
        select(User).where(User.email == payload.email.lower(), User.deleted_at.is_(None))
    ).scalar_one_or_none()

    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email address or password."
    )

    if user is None or not user.hashed_password:
        _register_failure(db, None, payload.email, meta)
        raise invalid

    if user.locked_until and user.locked_until > utcnow():
        remaining = int((user.locked_until - utcnow()).total_seconds() // 60) + 1
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"Account temporarily locked after repeated failed attempts. "
                   f"Try again in {remaining} minute(s).",
        )

    if not verify_password(payload.password, user.hashed_password):
        _register_failure(db, user, payload.email, meta)
        raise invalid

    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This account is {user.status}. Contact your training administrator.",
        )

    if needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(payload.password)

    if user.mfa_enabled:
        challenge = create_token(user.id, "mfa_challenge")
        return MfaChallenge(challenge_token=challenge)

    tokens = _issue_tokens(
        db, user, device_id=payload.device_id, device_label=payload.device_label, meta=meta
    )
    audit.record(
        db,
        action=AuditAction.LOGIN,
        tenant_id=user.tenant_id,
        actor_id=user.id,
        actor_label=user.full_name,
        entity_type="user",
        entity_id=user.id,
        summary="Signed in",
        **meta,
    )
    db.commit()
    return tokens


@router.post("/mfa/verify", response_model=TokenPair, summary="Complete MFA sign-in")
def verify_mfa(payload: MfaVerifyRequest, db: DbSession, meta: ClientMeta):
    try:
        claims = decode_token(payload.challenge_token, expected_type="mfa_challenge")
    except TokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    user = db.get(User, claims["sub"])
    if user is None or not user.mfa_enabled or not user.mfa_secret:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Multi-factor authentication is not configured for this account.")

    code = payload.code.strip()
    accepted = verify_mfa_code(user.mfa_secret, code)

    if not accepted:
        # Fall back to single-use recovery codes.
        for stored in list(user.mfa_recovery_hashes or []):
            if check_recovery_code(code, stored):
                user.mfa_recovery_hashes = [
                    h for h in user.mfa_recovery_hashes if h != stored
                ]
                accepted = True
                break

    if not accepted:
        _register_failure(db, user, user.email, meta)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="That code is not valid or has expired.")

    tokens = _issue_tokens(
        db, user, device_id=payload.device_id, device_label=payload.device_label, meta=meta
    )
    audit.record(db, action=AuditAction.LOGIN, tenant_id=user.tenant_id, actor_id=user.id,
                 actor_label=user.full_name, entity_type="user", entity_id=user.id,
                 summary="Signed in with MFA", **meta)
    db.commit()
    return tokens


@router.post("/refresh", response_model=TokenPair, summary="Exchange a refresh token")
def refresh(payload: RefreshRequest, db: DbSession, meta: ClientMeta):
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    session = db.execute(
        select(UserSession).where(UserSession.jti == claims["jti"])
    ).scalar_one_or_none()
    if session is None or session.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="This session has been signed out.")

    user = db.get(User, claims["sub"])
    if user is None or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account unavailable.")

    # Rotate the refresh token so a stolen one cannot be replayed after use.
    session.revoked_at = utcnow()
    db.add(session)
    tokens = _issue_tokens(db, user, device_id=None, device_label=session.device_label, meta=meta)
    db.commit()
    return tokens


@router.post("/logout", summary="Sign out of the current session")
def logout(payload: RefreshRequest, db: DbSession, principal: CurrentPrincipal, meta: ClientMeta):
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except TokenError:
        return {"detail": "Signed out."}

    session = db.execute(
        select(UserSession).where(UserSession.jti == claims["jti"])
    ).scalar_one_or_none()
    if session is not None:
        session.revoked_at = utcnow()
        db.add(session)
    audit.record(db, action=AuditAction.LOGOUT, tenant_id=principal.tenant_id,
                 actor_id=principal.id, entity_type="user", entity_id=principal.id,
                 summary="Signed out", **meta)
    db.commit()
    return {"detail": "Signed out."}


@router.get("/me", response_model=MeResponse, summary="The signed-in user and their access")
def me(db: DbSession, principal: CurrentPrincipal):
    user = principal.user
    tenant = db.get(Tenant, user.tenant_id) if user.tenant_id else None

    assignments = []
    rows = db.execute(
        select(RoleAssignment).where(RoleAssignment.user_id == user.id)
    ).scalars().all()
    for row in rows:
        org = db.get(OrgUnit, row.org_unit_id) if row.org_unit_id else None
        assignments.append(
            RoleAssignmentOut(
                id=row.id,
                role_id=row.role_id,
                role_code=row.role.code if row.role else None,
                role_name=row.role.name if row.role else None,
                org_unit_id=row.org_unit_id,
                org_unit_name=org.name if org else None,
                is_primary=row.is_primary,
                starts_on=row.starts_on,
                ends_on=row.ends_on,
            )
        )

    enrolment = db.execute(
        select(Enrolment)
        .where(Enrolment.trainee_id == user.id, Enrolment.deleted_at.is_(None))
        .order_by(Enrolment.start_date.desc())
    ).scalars().first()

    return MeResponse(
        user=UserOut.model_validate(user),
        tenant=(
            {
                "id": tenant.id,
                "name": tenant.name,
                "code": tenant.code,
                "branding": tenant.branding,
                "accrediting_bodies": tenant.accrediting_bodies,
                "settings": tenant.settings,
            }
            if tenant
            else None
        ),
        roles=assignments,
        permissions=sorted(principal.grants.keys()) if not principal.is_superuser else ["*"],
        is_superuser=principal.is_superuser,
        enrolment=(
            {
                "id": enrolment.id,
                "programme_id": enrolment.programme_id,
                "programme_name": enrolment.programme.name if enrolment.programme else None,
                "curriculum_version_id": enrolment.curriculum_version_id,
                "org_unit_id": enrolment.org_unit_id,
                "current_year": enrolment.current_year,
                "current_level": enrolment.current_level,
                "status": enrolment.status,
                "start_date": str(enrolment.start_date),
                "expected_end_date": str(enrolment.expected_end_date),
                "latest_overall_score": enrolment.latest_overall_score,
                "latest_rag": enrolment.latest_rag,
                "promotion_ready": enrolment.promotion_ready,
            }
            if enrolment
            else None
        ),
    )


@router.post("/password", summary="Change your own password")
def change_password(payload: PasswordChangeRequest, db: DbSession,
                    principal: CurrentPrincipal, meta: ClientMeta):
    user = principal.user
    if not user.hashed_password or not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Your current password is not correct.")
    try:
        enforce_password_policy(payload.new_password)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    user.hashed_password = hash_password(payload.new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = False
    db.add(user)

    # Every other session is invalidated — a password change should end stolen sessions.
    for session in db.execute(
        select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
    ).scalars():
        session.revoked_at = utcnow()
        db.add(session)

    audit.record(db, action=AuditAction.UPDATE, tenant_id=user.tenant_id, actor_id=user.id,
                 entity_type="user", entity_id=user.id, summary="Changed password", **meta)
    db.commit()
    return {"detail": "Password updated. Please sign in again on your other devices."}


@router.post("/mfa/enrol", response_model=MfaEnrolResponse, summary="Begin MFA enrolment")
def enrol_mfa(db: DbSession, principal: CurrentPrincipal):
    user = principal.user
    secret = generate_mfa_secret()
    codes = generate_recovery_codes()
    user.mfa_secret = secret
    user.mfa_recovery_hashes = [hash_recovery_code(c) for c in codes]
    db.add(user)
    db.commit()
    return MfaEnrolResponse(
        secret=secret,
        provisioning_uri=mfa_provisioning_uri(secret, user.email),
        recovery_codes=codes,
    )


@router.post("/mfa/activate", summary="Confirm and activate MFA")
def activate_mfa(payload: MfaVerifyRequest, db: DbSession, principal: CurrentPrincipal,
                 meta: ClientMeta):
    user = principal.user
    if not user.mfa_secret:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Start MFA enrolment before activating it.")
    if not verify_mfa_code(user.mfa_secret, payload.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="That code is not valid. Check your device clock and try again.")
    user.mfa_enabled = True
    db.add(user)
    audit.record(db, action=AuditAction.CONFIG_CHANGE, tenant_id=user.tenant_id, actor_id=user.id,
                 entity_type="user", entity_id=user.id,
                 summary="Enabled multi-factor authentication", **meta)
    db.commit()
    return {"detail": "Multi-factor authentication is now active on your account."}


@router.get("/sessions", summary="List your active sessions")
def list_sessions(db: DbSession, principal: CurrentPrincipal):
    rows = db.execute(
        select(UserSession)
        .where(UserSession.user_id == principal.id, UserSession.revoked_at.is_(None))
        .order_by(UserSession.created_at.desc())
    ).scalars().all()
    return [
        {
            "id": s.id,
            "device_label": s.device_label,
            "ip_address": s.ip_address,
            "user_agent": s.user_agent,
            "created_at": s.created_at,
            "expires_at": s.expires_at,
        }
        for s in rows
    ]


@router.delete("/sessions/{session_id}", summary="Revoke a session")
def revoke_session(session_id: str, db: DbSession, principal: CurrentPrincipal):
    session = db.get(UserSession, session_id)
    if session is None or session.user_id != principal.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    session.revoked_at = utcnow()
    db.add(session)
    db.commit()
    return {"detail": "Session revoked."}
