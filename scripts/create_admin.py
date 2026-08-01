#!/usr/bin/env python
"""Create the first institution and its administrator.

Created and managed by NEXORA Innovations.

Production deployments never run the demo seeder, so a fresh database has
reference data but no institution and no way in. This creates both::

    export RTC_DATABASE_URL='postgresql+psycopg://...:5432/postgres'
    export RTC_SECRET_KEY='...'
    python scripts/create_admin.py \\
        --institution "Federal Medical Centre, Owerri" --code FMC-OWE \\
        --email cmd@fmcowerri.gov.ng --name "Adaeze Nwachukwu"

The password is prompted for, never passed as an argument -- a command line ends
up in shell history and in the process table.
"""

from __future__ import annotations

import sys as _sys

# Windows consoles default to cp1252 and would raise on any non-ASCII
# output. Fall back rather than crash a deployment script over a glyph.
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(errors="replace")
    _sys.stderr.reconfigure(errors="replace")

import argparse
import getpass
import re
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.rbac import DEFAULT_ROLES  # noqa: E402
from app.core.security import (  # noqa: E402
    PasswordPolicyError,
    enforce_password_policy,
    hash_password,
)
from app.db.base import utcnow  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.enums import OrgKind, UserStatus  # noqa: E402
from app.models.identity import Role, RoleAssignment, User  # noqa: E402
from app.models.tenancy import OrgUnit, Tenant  # noqa: E402


class Abort(SystemExit):
    def __init__(self, message: str) -> None:
        super().__init__(f"\n  [!!] {message}\n")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:64]


def prompt_password() -> str:
    while True:
        first = getpass.getpass("  Password: ")
        try:
            enforce_password_policy(first)
        except PasswordPolicyError as exc:
            print(f"    {exc}")
            continue
        if first != getpass.getpass("  Confirm:  "):
            print("    The passwords do not match.")
            continue
        return first


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the first institution and administrator.",
        epilog="Created and managed by NEXORA Innovations.",
    )
    parser.add_argument("--institution", required=True, help="Full institution name.")
    parser.add_argument("--code", required=True, help="Short code, e.g. FMC-OWE.")
    parser.add_argument("--email", required=True, help="Administrator email address.")
    parser.add_argument("--name", required=True, help='Administrator name, e.g. "Ada Obi".')
    parser.add_argument("--title", default="Prof.", help="Title. Default: Prof.")
    parser.add_argument("--role", default="chief_medical_director",
                        choices=[r.code for r in DEFAULT_ROLES],
                        help="Role to grant. Default: chief_medical_director.")
    parser.add_argument("--country", default="NG")
    parser.add_argument("--timezone", default="Africa/Lagos")
    parser.add_argument("--generate-password", action="store_true",
                        help="Generate a password and print it once instead of prompting.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        role = db.execute(
            select(Role).where(Role.code == args.role, Role.tenant_id.is_(None))
        ).scalar_one_or_none()
        if role is None:
            raise Abort(
                f"Role '{args.role}' is not in the database.\n\n"
                "    Load reference data first:\n"
                "      python scripts/supabase_bootstrap.py --reference-data"
            )

        email = args.email.strip().lower()
        if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
            raise Abort(f"An account already exists for {email}.")

        tenant = db.execute(
            select(Tenant).where(Tenant.code == args.code)
        ).scalar_one_or_none()

        if tenant is None:
            tenant = Tenant(
                name=args.institution,
                code=args.code,
                slug=slugify(args.institution),
                kind=OrgKind.HOSPITAL,
                country=args.country,
                timezone=args.timezone,
                accrediting_bodies=["npmcn", "mdcn"],
                branding={"primary": "#166534", "accent": "#b45309",
                          "logo_text": args.code.split("-")[0][:4]},
                settings={"academic_year_start_month": 7,
                          "logbook_validation_sla_days": 7,
                          "minimum_academic_attendance_percent": 75},
            )
            db.add(tenant)
            db.flush()
            print(f"  [ok] Created institution '{tenant.name}' [{tenant.code}]")
        else:
            print(f"  - Using existing institution '{tenant.name}' [{tenant.code}]")

        root = db.execute(
            select(OrgUnit).where(OrgUnit.tenant_id == tenant.id, OrgUnit.parent_id.is_(None))
        ).scalar_one_or_none()
        if root is None:
            root = OrgUnit(
                tenant_id=tenant.id, kind=OrgKind.HOSPITAL, name=tenant.name,
                code=f"{tenant.code}-ROOT", path=f"/{tenant.code}-ROOT", depth=0,
            )
            db.add(root)
            db.flush()
            print(f"  [ok] Created root organisational unit '{root.code}'")

        if args.generate_password:
            password = secrets.token_urlsafe(16) + "aA1!"
        else:
            print(f"\n  Set a password for {email}")
            password = prompt_password()

        parts = args.name.split()
        user = User(
            tenant_id=tenant.id,
            email=email,
            title=args.title,
            first_name=parts[0],
            last_name=parts[-1] if len(parts) > 1 else parts[0],
            middle_name=" ".join(parts[1:-1]) or None,
            hashed_password=hash_password(password),
            status=UserStatus.ACTIVE,
            email_verified_at=utcnow(),
            password_changed_at=utcnow(),
            # Generated passwords are transmitted to a human somehow; force a
            # change so the one that travelled is not the one that persists.
            must_change_password=args.generate_password,
        )
        db.add(user)
        db.flush()

        db.add(RoleAssignment(user_id=user.id, role_id=role.id,
                              org_unit_id=root.id, is_primary=True))
        db.commit()

        print(f"  [ok] Created {user.full_name} as {role.name}")
        if args.generate_password:
            print(f"\n  Password (shown once): {password}")
            print("  They will be required to change it at first sign-in.")
        print(
            "\n  Sign in, then:\n"
            "    1. Administration -> Branding -- upload the crest and set colours.\n"
            "    2. Institutions -> build the faculty and department hierarchy.\n"
            "    3. Enable multi-factor authentication on this account.\n"
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
