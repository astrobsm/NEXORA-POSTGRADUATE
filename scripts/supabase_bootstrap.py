#!/usr/bin/env python
"""Prepare a Supabase database for the Postgraduate Medical Training Console.

Created and managed by NEXORA Innovations.

Run this once, from your machine or from CI, against a fresh Supabase project::

    export RTC_DATABASE_URL='postgresql+psycopg://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres'
    export RTC_SECRET_KEY='<a long random string>'
    python scripts/supabase_bootstrap.py --reference-data

It will:

1. Verify it can reach the database, and refuse to continue against the
   *transaction* pooler -- Alembic issues DDL and multi-statement transactions,
   which PgBouncer in transaction mode cannot serve reliably.
2. Apply every Alembic migration.
3. Optionally load platform reference data (permissions, roles, the specialty
   catalogue, accreditation standards) and, if you insist, the demo institution.

It deliberately does *not* create a Supabase project, set up auth, or touch
storage. RTC uses Supabase only as PostgreSQL; its own authentication, RBAC and
audit trail are the security model, and layering Supabase Auth on top would give
you two sources of truth about who a user is.
"""

from __future__ import annotations

import sys as _sys

# Windows consoles default to cp1252 and would raise on any non-ASCII
# output. Fall back rather than crash a deployment script over a glyph.
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(errors="replace")
    _sys.stderr.reconfigure(errors="replace")

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))


class Abort(SystemExit):
    def __init__(self, message: str) -> None:
        super().__init__(f"\n  [!!] {message}\n")


def step(message: str) -> None:
    print(f"  -> {message}")


def ok(message: str) -> None:
    print(f"  [ok] {message}")


# --------------------------------------------------------------------------
def check_environment() -> str:
    url = os.getenv("RTC_DATABASE_URL", "")
    if not url:
        raise Abort(
            "RTC_DATABASE_URL is not set.\n\n"
            "    Supabase -> Project Settings -> Database -> Connection string -> URI.\n"
            "    Use the SESSION pooler (port 5432) for this script, and swap the\n"
            "    driver prefix to `postgresql+psycopg://`."
        )

    if url.startswith("sqlite"):
        raise Abort("RTC_DATABASE_URL points at SQLite. This script is for Supabase.")

    if ":6543" in url:
        raise Abort(
            "That is the transaction pooler (port 6543).\n\n"
            "    Migrations need the SESSION pooler on port 5432: Alembic runs DDL\n"
            "    inside multi-statement transactions, and PgBouncer's transaction\n"
            "    mode hands each statement a different backend connection.\n\n"
            "    Use 6543 for the deployed application, 5432 for migrations."
        )

    if not os.getenv("RTC_SECRET_KEY"):
        raise Abort(
            "RTC_SECRET_KEY is not set. Sessions are signed with it.\n\n"
            "    Generate one:  python -c \"import secrets;print(secrets.token_urlsafe(48))\""
        )

    return url


def check_connection(url: str) -> None:
    from sqlalchemy import create_engine, text

    step("Connecting to Supabase...")
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as connection:
            version = connection.execute(text("SHOW server_version")).scalar_one()
            database = connection.execute(text("SELECT current_database()")).scalar_one()
    except Exception as exc:
        raise Abort(
            f"Could not connect: {exc}\n\n"
            "    Check the password is URL-encoded if it contains @ : / or #,\n"
            "    and that your IP is permitted under Database -> Network Restrictions."
        ) from exc
    ok(f"PostgreSQL {version}, database '{database}'")


def run_migrations() -> None:
    step("Applying migrations...")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise Abort(f"Migration failed:\n\n{result.stderr.strip()}")
    ok("Schema is at head")


def check_drift() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            "  ! The models have drifted from the migrations. Generate one:\n"
            "      cd backend && alembic revision --autogenerate -m 'describe the change'"
        )
    else:
        ok("Models and migrations agree")


def seed(demo: bool) -> None:
    from app.db.seed import seed as run_seed

    step("Seeding demo institution..." if demo else "Loading reference data...")
    run_seed(demo=demo, reset=False)


def report() -> None:
    from sqlalchemy import func, select

    from app.db.session import SessionLocal
    from app.models.curriculum import Specialty
    from app.models.identity import Permission, Role
    from app.models.tenancy import Tenant

    db = SessionLocal()
    try:
        counts = {
            "institutions": db.execute(select(func.count()).select_from(Tenant)).scalar_one(),
            "permissions": db.execute(select(func.count()).select_from(Permission)).scalar_one(),
            "roles": db.execute(select(func.count()).select_from(Role)).scalar_one(),
            "specialties": db.execute(select(func.count()).select_from(Specialty)).scalar_one(),
        }
    finally:
        db.close()

    print("\n  Database contents:")
    for label, value in counts.items():
        print(f"    {label:<14} {value}")


# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare a Supabase database for the Postgraduate Medical Training Console.",
        epilog="Created and managed by NEXORA Innovations.",
    )
    parser.add_argument("--reference-data", action="store_true",
                        help="Load permissions, roles, specialties and accreditation standards.")
    parser.add_argument("--demo", action="store_true",
                        help="Also create the demo institution. Never do this in production.")
    parser.add_argument("--migrate-only", action="store_true",
                        help="Apply migrations and stop.")
    args = parser.parse_args()

    print("\n  Postgraduate Medical Training Console -- Supabase bootstrap")
    print("  " + "-" * 58)

    url = check_environment()
    check_connection(url)
    run_migrations()
    check_drift()

    if args.migrate_only:
        print("\n  Done -- schema only.\n")
        return

    if args.demo:
        if os.getenv("RTC_ENV") == "production":
            raise Abort("Refusing to seed demo data with RTC_ENV=production.")
        print(
            "\n  ! The demo institution creates accounts with a published password.\n"
            "    Only do this on a throwaway project."
        )
        if input("    Type 'yes' to continue: ").strip().lower() != "yes":
            raise Abort("Aborted.")
        seed(demo=True)
    elif args.reference_data:
        seed(demo=False)

    report()

    print(
        "\n  Next:\n"
        "    1. Set RTC_DATABASE_URL in Vercel to the TRANSACTION pooler (port 6543).\n"
        "    2. Set RTC_SECRET_KEY, RTC_ENV=production, RTC_ALLOW_DEMO_SEED=false.\n"
        "    3. Create your first administrator -- see docs/DEPLOYMENT_VERCEL.md.\n"
    )


if __name__ == "__main__":
    main()
