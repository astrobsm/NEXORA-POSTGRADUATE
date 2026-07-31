"""Password hashing, JWT issuance/verification and TOTP multi-factor helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import settings

_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)

TokenType = Literal["access", "refresh", "mfa_challenge", "invite", "reset"]


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, raw)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(hashed: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed)
    except (InvalidHashError, ValueError):
        return True


class PasswordPolicyError(ValueError):
    """Raised when a candidate password fails institutional policy."""


def enforce_password_policy(raw: str) -> None:
    """Institutional baseline. Length is configurable per deployment."""
    problems: list[str] = []
    if len(raw) < settings.password_min_length:
        problems.append(f"at least {settings.password_min_length} characters")
    if not any(c.islower() for c in raw):
        problems.append("a lowercase letter")
    if not any(c.isupper() for c in raw):
        problems.append("an uppercase letter")
    if not any(c.isdigit() for c in raw):
        problems.append("a digit")
    if not any(not c.isalnum() for c in raw):
        problems.append("a symbol")
    if problems:
        raise PasswordPolicyError("Password must contain " + ", ".join(problems) + ".")


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(UTC)


def create_token(
    subject: str,
    token_type: TokenType,
    *,
    expires_delta: timedelta | None = None,
    claims: dict[str, Any] | None = None,
) -> str:
    if expires_delta is None:
        expires_delta = {
            "access": timedelta(minutes=settings.access_token_ttl_minutes),
            "refresh": timedelta(days=settings.refresh_token_ttl_days),
            "mfa_challenge": timedelta(minutes=5),
            "invite": timedelta(days=7),
            "reset": timedelta(hours=2),
        }[token_type]

    issued_at = _now()
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + expires_delta,
        "jti": uuid.uuid4().hex,
        "iss": settings.app_short_name,
    }
    if claims:
        payload.update(claims)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


class TokenError(Exception):
    """Raised when a token is absent, malformed, expired or of the wrong type."""


def decode_token(token: str, *, expected_type: TokenType | None = None) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.app_short_name,
        )
    except jwt.ExpiredSignatureError as exc:  # pragma: no cover - trivial
        raise TokenError("Token has expired.") from exc
    except jwt.PyJWTError as exc:
        raise TokenError("Token is invalid.") from exc

    if expected_type and payload.get("typ") != expected_type:
        raise TokenError(f"Expected a {expected_type} token.")
    return payload


# --------------------------------------------------------------------------
# Multi-factor authentication (TOTP)
# --------------------------------------------------------------------------
def generate_mfa_secret() -> str:
    return pyotp.random_base32()


def mfa_provisioning_uri(secret: str, account_name: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(
        name=account_name, issuer_name=settings.mfa_issuer
    )


def verify_mfa_code(secret: str, code: str, *, valid_window: int = 1) -> bool:
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=valid_window)


def generate_recovery_codes(count: int = 10) -> list[str]:
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(count)]


def hash_recovery_code(code: str) -> str:
    digest = hashlib.sha256(f"{settings.secret_key}:{code}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()


def check_recovery_code(code: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_recovery_code(code), hashed)


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------
def new_id() -> str:
    """UUID4 hex — stable across SQLite and PostgreSQL without dialect types."""
    return uuid.uuid4().hex
