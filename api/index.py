"""Vercel serverless entrypoint for the Postgraduate Medical Training Console API.

Created and managed by NEXORA Technologies.

Vercel's Python runtime discovers an ASGI application exported as ``app`` from a
module under ``/api``. The application itself lives in ``/backend`` so that it
stays a normal, runnable FastAPI project — this file only puts it on the path.

Read ``docs/DEPLOYMENT_VERCEL.md`` before deploying. Two constraints matter:

* **Migrations do not run here.** A serverless function can be invoked
  concurrently dozens of times on a cold deploy; running Alembic in each one is a
  race against the schema. Migrations run from CI — see
  ``.github/workflows/migrate.yml``.
* **Long jobs do not run here either.** Cohort score recomputation iterates every
  trainee and will exceed the function timeout on any plan. It runs on a
  schedule from CI instead — see ``.github/workflows/nightly.yml``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The backend package root, so `import app...` resolves inside the function bundle.
BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.main import app  # noqa: E402  (path setup must precede the import)

__all__ = ["app"]
