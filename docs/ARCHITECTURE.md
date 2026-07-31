# Architecture

## What this system is

RTC manages the *training* of postgraduate doctors and dentists: what a trainee has
done, what their curriculum requires, whether they may progress, and whether the
department that trains them meets its accrediting body's standards.

It is not a clinical system. It holds no patient-identifiable data — logbook entries
reference a pseudonymous token, an age and a sex, and nothing more.

---

## The one idea worth understanding

**Training policy is data, not code.**

A residency programme's rules — 40 major operations in year 2, 75% attendance at
grand rounds, a dissertation past ethics approval before promotion — are stored as
`RequirementRule` rows. One engine evaluates them. Change the rule, and the promotion
engine, the analytics, the trainee's dashboard and the accreditation return all change
with it, with no deployment.

```
RequirementRule                       app.services.requirements
┌──────────────────────────┐          ┌────────────────────────────────┐
│ kind:      procedure_role_count  ──▶│ MEASURERS[kind](ctx, rule)     │
│ operator:  gte                      │   → queries the logbook        │
│ target:    40                       │   → returns a Measurement      │
│ scope:     training_year            ├────────────────────────────────┤
│ params:    {role: performed_...,    │ evaluate_rule()                │
│             grade: major,           │   → applies operator + target  │
│             training_year: 2}       │   → RequirementResult          │
│ severity:  mandatory                │     (met, measured, target,    │
│ domain:    clinical_competency      │      shortfall, progress, why) │
└──────────────────────────┘          └────────────────────────────────┘
                                                    │
        ┌───────────────────────────┬───────────────┼──────────────────┐
        ▼                           ▼               ▼                  ▼
   scoring.py                 promotion.py     rotation.py     the trainee's
   (8 domain scores)          (gates)          (sign-off)      dashboard
```

Code changes only when a genuinely new *kind of measurement* is invented — and then
the change is one function plus one enum member.

---

## Shape

```
┌──────────────────────────── Browser ────────────────────────────┐
│  React 19 PWA                                                    │
│  ├── React Query      server state, cache, retry                 │
│  ├── Dexie/IndexedDB  offline mirror + write outbox              │
│  ├── Sync engine      push-then-pull, revision conflicts         │
│  └── Service worker   shell cache, network-first API             │
└────────────────────────────────┬────────────────────────────────┘
                                 │ HTTPS · JWT bearer
┌────────────────────────────────▼────────────────────────────────┐
│  Nginx — one origin for app and API                              │
└────────────────────────────────┬────────────────────────────────┘
┌────────────────────────────────▼────────────────────────────────┐
│  FastAPI                                                         │
│  ├── api/v1/         routing, permission checks, serialisation   │
│  ├── services/       the engines (no HTTP concepts)              │
│  ├── models/         SQLAlchemy domain schema                    │
│  └── core/           config, security, RBAC catalogue            │
└──────┬───────────────────────────────────────────┬──────────────┘
       ▼                                           ▼
┌──────────────┐                          ┌──────────────────┐
│ PostgreSQL   │                          │ S3-compatible    │
│ 62 tables    │                          │ object storage   │
└──────────────┘                          └──────────────────┘
```

### Layering rule

`api` → `services` → `models`. Services never import from `api`; they take a
`Session` and domain objects and return dataclasses. That is what lets the same
promotion logic run from an HTTP request, a nightly job, and a test with no HTTP at
all.

---

## Multi-tenancy

Two mechanisms, deliberately separate:

**`Tenant`** is the institution. Every scoped row carries `tenant_id`, and every
read path filters on it.

**`OrgUnit`** is the eight-level ladder — National → College → Hospital → Faculty →
Department → Unit → Subspecialty → Programme — modelled as *one* self-referential
tree rather than eight tables. An institution uses as many levels as it needs; a
single-department pilot uses two.

`path` is a materialised ancestor path (`/UTH/FAC-CLIN/DEPT-SURG`), so a subtree
query is one indexed `LIKE 'path/%'` and works identically on SQLite and PostgreSQL.

### Permissions follow the tree

A `RoleAssignment` binds (user, role, org unit). A permission held at a node applies
throughout its subtree and nowhere else. `build_principal()` resolves this once per
request into `{permission → {org unit ids}}`; `Principal.has(perm, org_unit_id=…)` is
then a set lookup.

This is why a consultant in Surgery cannot read Paediatric logbooks, and why a Head of
Department sees their department's analytics but not the institution's.

**Rows with a NULL owner are shared with everyone** — system roles, the specialty
catalogue, accreditation standards. Query them with `owned_or_shared()`, never
`column.in_([tenant_id, None])`: SQL's `IN` never matches NULL, so the obvious
spelling silently hides every shared row. `tests/test_shared_records.py` pins this.

---

## Offline-first

The design assumption is a registrar in a theatre with no signal who has just done
something that must be recorded.

### Writes

1. The form writes to the **outbox** (IndexedDB) with a client-generated UUID.
2. The user is told immediately: *saved on this device*.
3. When connectivity returns, the sync engine posts the batch.
4. The server de-duplicates on `client_uuid`, so a retry after a dropped connection
   cannot create a second entry.

### Reads

Reference data (curriculum, competencies, procedure catalogue, assessment
instruments) is mirrored, so forms render fully offline.

### Conflicts

Every syncable row has a `revision` that increments on write. A push carries the
revision the device last saw. If the server has moved on, the push is recorded as a
**conflict** and the server row is left untouched.

Last-write-wins is not acceptable here: a supervisor validating an entry while the
trainee edits it offline must not have their sign-off silently overwritten.

### Push before pull

Always. Pulling first would overwrite local unsynced work with the server's older
view of it.

### What the service worker does *not* do

It never caches or replays a mutating request. Offline write semantics live in the
outbox, which understands client UUIDs and conflicts. A service worker replaying a
POST blindly could duplicate a clinical record.

---

## The engines

| Module | Responsibility |
|---|---|
| `services/requirements.py` | Measure and evaluate every `RequirementRule`. The core. |
| `services/scoring.py` | Roll requirement results into eight domain scores + RAG. |
| `services/promotion.py` | Promotion gates, exam eligibility, decision recording. |
| `services/rotation.py` | Schedule generation, extension, remediation, leave. |
| `services/allocation.py` | Supervisor matching with recorded reasoning. |
| `services/accreditation.py` | Evaluate a body's standard over a department. |
| `services/notifications.py` | Rule-driven, template-driven messaging. |
| `services/audit.py` | Append-only trail with secret redaction. |

### Two decisions worth flagging

**Unassessed ≠ zero.** If a curriculum defines no leadership requirement, leadership
is reported as *not assessed* and excluded from the weighted overall score. Scoring it
zero would penalise every trainee for a curriculum-authoring omission.

**Nothing counts until it is validated.** Only `validation_status == validated`
logbook entries are measured. A trainee cannot inflate their own record; a validated
entry is then locked, because it is evidence a college may inspect.

---

## Data integrity

- **Curriculum versions are pinned.** A trainee is measured against the version in
  force when they enrolled. Publishing a new curriculum never retroactively changes
  anyone's requirements.
- **Score snapshots are immutable.** Trends are real history, not a recomputation.
- **Competency ratings are append-only.** Progression is the latest rating per
  competency; the history stays.
- **Promotion overrides require a reason.** A decision contradicting the engine is
  accepted — committees have information the system does not — but the write is
  refused without a recorded justification.

---

## Technology choices

| Choice | Why |
|---|---|
| FastAPI | Typed request/response with OpenAPI for free; the API doc is generated, so it cannot drift. |
| SQLAlchemy 2.0 | Explicit, typed ORM. Same models drive Alembic autogeneration. |
| SQLite by default | The platform runs with zero infrastructure. PostgreSQL for anything real; CI tests both. |
| UUID hex primary keys | Offline devices mint ids without coordination; no sequence contention. |
| JSON columns for config | `settings`, `parameters`, `form_schema` change per institution. Columns would mean a migration per policy tweak. |
| React Query + Dexie | Server state and offline state are different problems; conflating them is where offline apps go wrong. |
| Tailwind v4 + CSS custom properties | Institutions white-label. Components read `var(--brand)`, not a hard-coded class. |

---

## Known limits

See [ROADMAP.md](ROADMAP.md) for what is complete versus scaffolded. In short: the
training, logbook, assessment, analytics, promotion, rotation, research and
accreditation paths are implemented and tested end to end. Report *rendering*
(PDF/XLSX), the CBT delivery UI, WebSocket push and the AI assistant have schema and
API surface but no implementation behind them.
