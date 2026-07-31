#!/usr/bin/env sh
# --------------------------------------------------------------------------
# Container entrypoint.
#
# Waits for the database, applies migrations, optionally seeds, then execs the
# real command. Migrations run here rather than in a separate job so a rolling
# deploy cannot start a container against a schema it does not understand.
# --------------------------------------------------------------------------
set -eu

log() { printf '[entrypoint] %s\n' "$1"; }

# ---- Wait for PostgreSQL --------------------------------------------------
if printf '%s' "${RTC_DATABASE_URL:-}" | grep -q '^postgres'; then
  log "Waiting for PostgreSQL…"
  attempt=0
  until python - <<'PY' 2>/dev/null
import os, sys
from sqlalchemy import create_engine, text
url = os.environ["RTC_DATABASE_URL"]
try:
    create_engine(url, pool_pre_ping=True).connect().execute(text("SELECT 1"))
except Exception:
    sys.exit(1)
PY
  do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
      log "Database did not become available after 60 attempts. Giving up."
      exit 1
    fi
    sleep 2
  done
  log "Database is available."
fi

# ---- Migrate --------------------------------------------------------------
if [ "${RTC_RUN_MIGRATIONS:-true}" = "true" ]; then
  log "Applying database migrations…"
  alembic upgrade head
  log "Migrations applied."
fi

# ---- Seed (opt-in) --------------------------------------------------------
# Guarded twice: the flag must be set AND the environment must not be production.
if [ "${RTC_SEED_ON_START:-false}" = "true" ] && [ "${RTC_ENV:-local}" != "production" ]; then
  log "Seeding reference data and the demo institution…"
  python -m app.db.seed || log "Seeding skipped or already applied."
fi

log "Starting: $*"
exec "$@"
