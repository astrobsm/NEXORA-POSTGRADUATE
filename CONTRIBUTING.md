# Contributing

Postgraduate Medical Training Console — created and managed by **NEXORA Innovations**.

---

## Before you start

```bash
git clone https://github.com/astrobsm/NEXORA-POSTGRADUATE.git
cd NEXORA-POSTGRADUATE
make install          # or follow the manual steps in README.md
make seed             # demo institution on SQLite, no infrastructure needed
make dev-api          # :8000
make dev-web          # :5173
```

`make check` runs everything CI runs. Run it before opening a pull request.

---

## What this codebase cares about

Three rules carry most of the weight. Breaking them is how this system would
stop being trustworthy.

### 1. Policy is data

Training requirements, assessment instruments, notification rules and
accreditation standards are **rows**, not code. If you find yourself writing
`if programme.code == "SURG-RES"`, stop — that belongs in a `RequirementRule`.

The only legitimate reason to add code to `services/requirements.py` is a
genuinely new *kind of measurement*. When you do, add the enum member, the
measurer, and the `MEASURERS` registration together —
`test_every_declared_kind_has_a_measurer` will fail if you forget the third.

### 2. Nothing counts until it is validated

Only logbook entries with `validation_status == validated` are measured. A
trainee cannot inflate their own record. A validated entry is then **locked**,
because it is evidence a college may inspect.

If you add a new source of evidence, decide explicitly who validates it.

### 3. The server is the authority

Client-side permission checks decide what is *displayed*. The server decides what
is *permitted*, on every request. Never add a code path where the client's answer
is load-bearing.

---

## Things that have bitten us

Learn from these rather than rediscovering them.

**`column.in_([tenant_id, None])` is silently wrong.** SQL's `IN` compares with
`=`, and `NULL = NULL` is unknown, so every shared platform-level row disappears.
Use `owned_or_shared()`. Pinned by `tests/test_shared_records.py`.

**An unassessed domain is not a zero.** A curriculum that defines no leadership
requirement must not score every trainee zero for leadership. Unassessed domains
are excluded from the overall score and the weights renormalised.

**A permission nobody holds is a dead feature.** Adding to `PERMISSIONS` without
granting it to a role means the endpoint is unreachable. This has happened twice.

**Week-based schedules drift past month-based year boundaries.** When a rotation
belongs to training year N, select it by `training_year`, not by date arithmetic.

---

## Code style

**Python** — Ruff, 100 columns, full type annotations. `make lint` and
`make format`.

**TypeScript** — strict mode, no `any` in new code where a type is knowable.
`make typecheck`.

**Comments explain *why*.** The code already says what it does. A comment that
restates the line below it is noise; a comment explaining why the obvious
approach was rejected is the most valuable thing in the file.

**Error messages are read by clinicians.** `"Decision must be 'approved' or
'rejected'."` is good. `"Invalid enum value"` is not.

---

## Tests

Every pull request needs tests. In rough order of value:

1. **A regression test for the bug you fixed**, written so it fails before your fix.
2. **Security boundaries** — who can and cannot do the thing.
3. **The engine behaviour** — measured value, target, verdict.

Run `make test`. CI additionally runs the suite against PostgreSQL, because
SQLite and PostgreSQL disagree in ways that only show up in production.

---

## Database changes

```bash
make migration m="add teaching feedback score"
make migrate
make migration-check      # fails if the models drifted
```

CI runs `upgrade head` → `downgrade base` → `upgrade head` → `alembic check`. A
model change without a migration fails the build.

If your migration cannot restore data on downgrade — a dropped column, say —
say so in the pull request description.

---

## Pull requests

- One concern per pull request.
- Describe **what changed and why**, not a file list; the diff is the file list.
- Note anything a deployment must do: a migration, a new environment variable, a
  reseed of reference data.
- Screenshots for interface changes, in both light and dark mode.

---

## Reporting a vulnerability

Do **not** open a public issue. See [SECURITY.md](SECURITY.md).

---

© NEXORA Innovations. See [LICENSE](LICENSE) — this is proprietary software.
