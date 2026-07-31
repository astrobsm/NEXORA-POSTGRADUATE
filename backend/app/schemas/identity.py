"""Auth, user and role contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ApiModel


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    device_id: str | None = None
    device_label: str | None = None


class MfaChallenge(BaseModel):
    mfa_required: bool = True
    challenge_token: str
    message: str = "Enter the 6-digit code from your authenticator app."


class MfaVerifyRequest(BaseModel):
    challenge_token: str
    code: str = Field(min_length=6, max_length=12)
    device_id: str | None = None
    device_label: str | None = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class MfaEnrolResponse(BaseModel):
    secret: str
    provisioning_uri: str
    recovery_codes: list[str]


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
class RoleAssignmentOut(ApiModel):
    id: str
    role_id: str
    role_code: str | None = None
    role_name: str | None = None
    org_unit_id: str | None = None
    org_unit_name: str | None = None
    is_primary: bool = False
    starts_on: date | None = None
    ends_on: date | None = None


class UserOut(ApiModel):
    id: str
    tenant_id: str | None = None
    email: str
    title: str | None = None
    first_name: str
    middle_name: str | None = None
    last_name: str
    full_name: str
    display_name: str
    initials: str
    phone: str | None = None
    discipline: str
    registration_number: str | None = None
    staff_number: str | None = None
    status: str
    is_platform_admin: bool = False
    mfa_enabled: bool = False
    photo_key: str | None = None
    qualifications: list[dict[str, Any]] = Field(default_factory=list)
    college_memberships: list[dict[str, Any]] = Field(default_factory=list)
    preferences: dict[str, Any] = Field(default_factory=dict)
    last_login_at: datetime | None = None
    created_at: datetime


class MeResponse(BaseModel):
    user: UserOut
    tenant: dict[str, Any] | None = None
    roles: list[RoleAssignmentOut]
    permissions: list[str]
    is_superuser: bool
    #: Enrolment summary when the caller is a trainee.
    enrolment: dict[str, Any] | None = None


class UserCreate(BaseModel):
    email: EmailStr
    first_name: str
    last_name: str
    middle_name: str | None = None
    title: str | None = None
    phone: str | None = None
    discipline: str = "medical"
    registration_number: str | None = None
    staff_number: str | None = None
    password: str | None = None
    role_code: str | None = None
    org_unit_id: str | None = None
    send_invite: bool = True


class UserUpdate(BaseModel):
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    phone: str | None = None
    alt_phone: str | None = None
    registration_number: str | None = None
    registration_expiry: date | None = None
    staff_number: str | None = None
    qualifications: list[dict[str, Any]] | None = None
    college_memberships: list[dict[str, Any]] | None = None
    bio: str | None = None
    preferences: dict[str, Any] | None = None
    status: str | None = None


class RoleOut(ApiModel):
    id: str
    tenant_id: str | None = None
    code: str
    name: str
    description: str | None = None
    rank: int
    scope_kind: str
    is_system: bool
    is_trainee_role: bool
    is_supervisor_role: bool
    permission_codes: list[str]


class RoleCreate(BaseModel):
    code: str
    name: str
    description: str | None = None
    rank: int = 50
    scope_kind: str = "department"
    permission_codes: list[str] = Field(default_factory=list)
    is_trainee_role: bool = False
    is_supervisor_role: bool = False


class RoleAssignRequest(BaseModel):
    user_id: str
    role_id: str
    org_unit_id: str | None = None
    is_primary: bool = False
    starts_on: date | None = None
    ends_on: date | None = None
    notes: str | None = None


class SupervisorProfileOut(ApiModel):
    id: str
    user_id: str
    expertise: list[str]
    methodologies: list[str]
    max_supervisees: int
    max_clinical_trainees: int
    accepting_new: bool
    completed_supervisions: int


class SupervisorProfileUpsert(BaseModel):
    expertise: list[str] = Field(default_factory=list)
    methodologies: list[str] = Field(default_factory=list)
    max_supervisees: int = 5
    max_clinical_trainees: int = 8
    accepting_new: bool = True
    unavailable_from: date | None = None
    unavailable_to: date | None = None
    conflicts_of_interest: list[str] = Field(default_factory=list)
