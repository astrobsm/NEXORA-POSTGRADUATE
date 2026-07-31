<div align="center">

# Postgraduate Medical Training Console

**Residency Training Console (RTC)** — an offline-first, multi-tenant,
competency-based medical education platform for teaching hospitals, federal
medical centres, specialist hospitals, universities, medical and dental colleges,
and national residency authorities.

[![CI](https://github.com/astrobsm/NEXORA-POSTGRADUATE/actions/workflows/ci.yml/badge.svg)](https://github.com/astrobsm/NEXORA-POSTGRADUATE/actions/workflows/ci.yml)
[![Deploy](https://github.com/astrobsm/NEXORA-POSTGRADUATE/actions/workflows/deploy.yml/badge.svg)](https://github.com/astrobsm/NEXORA-POSTGRADUATE/actions/workflows/deploy.yml)
[![Licence](https://img.shields.io/badge/licence-proprietary-166534)](LICENSE)

**Created and managed by [NEXORA Technologies](https://github.com/astrobsm/NEXORA-POSTGRADUATE)**

</div>

---

RTC is not an electronic logbook. It is a **residency operating system**:
curriculum, rotations, duty, logbook, assessment, academics, research, analytics,
promotion and accreditation — all driven by *configuration* rather than code
changes.

## The one idea worth understanding

**Training policy is data.**

A programme's rules — 40 major operations in year 2, 75% attendance at grand
rounds, a dissertation past ethics approval before promotion — are stored as
rows. One engine evaluates them. Change the rule and the promotion engine, the
analytics, the trainee's dashboard and the accreditation return all change with
it, with no deployment.

> A registrar's dashboard does not say "you are behind". It says *which*
> requirement, measured against *what*, short by *how much*, and where the number
> came from.

---

## Compliance targets

| Body | Coverage |
|---|---|
| **NPMCN** — National Postgraduate Medical College of Nigeria | Programme structure, exam eligibility, accreditation returns |
| **WACS** — West African College of Surgeons | Fellowship tracks, procedure minima, dissertation workflow |
| **WACP** — West African College of Physicians | Fellowship tracks, academic activity minima |
| **MDCN** — Medical & Dental Council of Nigeria | Housemanship rotations, internship sign-off |
| **NUC** — National Universities Commission | Faculty and department academic returns |
| Royal Colleges / any other body | Fully user-definable in the accreditation profile builder |

---

## Quick start — no infrastructure

The backend defaults to SQLite, so it runs with no database server at all.

```bash
git clone https://github.com/astrobsm/NEXORA-POSTGRADUATE.git
cd NEXORA-POSTGRADUATE

# Backend
cd backend
python -m venv .venv && .venv/Scripts/activate     # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m app.db.seed                              # demo institution
uvicorn app.main:app --reload --port 8000

# Frontend, in a second terminal
cd frontend && npm install && npm run dev
```

**App** http://localhost:5173 · **API docs** http://localhost:8000/docs

Or the whole stack with PostgreSQL and object storage:

```bash
cp .env.example .env      # set RTC_SECRET_KEY
docker compose up --build # → http://localhost:8080
```

### Demo accounts

Password `RtcDemo!2026` for all of them — each role gets a different console.
The sign-in screen has a button for each.

| Role | Email |
|---|---|
| Chief Medical Director | `cmd@uthdemo.health` |
| Director of Residency Training | `drt@uthdemo.health` |
| Head of Department, Surgery | `hod.surgery@uthdemo.health` |
| Consultant | `consultant1@uthdemo.health` |
| Senior Registrar | `snr.registrar1@uthdemo.health` |
| Registrar | `registrar1@uthdemo.health` |
| House Officer | `houseofficer1@uthdemo.health` |

> Demo credentials exist for evaluation only. `RTC_ALLOW_DEMO_SEED` must be
> `false` in production, and the seeder refuses to run when `RTC_ENV=production`.

---

## What it does

**Curriculum builder** — specialties, programmes, versioned curricula, training
years, rotations, competencies and EPAs, and requirement rules across 20
measurement kinds.

**Digital logbook** — offline capture, consultant validation, bulk sign-off,
locking after validation, full audit trail. Nothing counts until it is validated.

**Rotation engine** — automatic scheduling with capacity warnings and supervisor
allocation; extension cascades, remedial postings, leave interruption.

**Assessment** — institution-designed instruments rendered dynamically, weighted
scoring, entrustment ratings against curriculum targets.

**Analytics** — eight domain scores with colour-coded status, immutable snapshots,
trends, and four role-specific dashboards.

**Promotion engine** — requirement, time-served, rotation and standing gates, each
with its reasoning shown. A committee can overturn it, but must record why.

**Accreditation** — 17 metrics, NPMCN/WACS/WACP/MDCN/NUC standards seeded, ranked
gaps and a plain-language narrative for a covering letter.

**Offline-first** — IndexedDB mirror, write outbox, revision-based conflict
detection. Record a case in theatre with no signal; it reconciles later.

**Institution branding** — each tenant uploads its own crest, app icon and colours;
the whole interface and the installed app icon follow.

---

## Deployment

| Target | Guide |
|---|---|
| **Vercel + Supabase** | [docs/DEPLOYMENT_VERCEL.md](docs/DEPLOYMENT_VERCEL.md) |
| Docker, Kubernetes, self-hosted | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |

> **On Vercel**, migrations and nightly score recomputation run from GitHub
> Actions, not from the serverless function — a cold deploy invokes the function
> concurrently, and several processes racing a migration corrupts a schema. The
> Vercel guide explains exactly what fits the serverless model and what does not.

---

## Documentation

| Document | Purpose |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, boundaries, offline strategy |
| [CONFIGURATION.md](docs/CONFIGURATION.md) | **The rules engine — how policy changes without code** |
| [DATA_MODEL.md](docs/DATA_MODEL.md) | 80 tables, ER diagrams, indexing |
| [API.md](docs/API.md) | REST reference and conventions |
| [ADAPTIVE_LEARNING.md](docs/ADAPTIVE_LEARNING.md) | **CBT, readiness, examination conduct, AI authoring and its editorial gate** |
| [DEPLOYMENT_VERCEL.md](docs/DEPLOYMENT_VERCEL.md) | Vercel + Supabase, step by step |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Docker, Kubernetes, backups, sizing |
| [SECURITY.md](docs/SECURITY.md) | RBAC, threat model, NDPR/GDPR posture |
| [USER_MANUAL.md](docs/USER_MANUAL.md) | Trainee, consultant, coordinator guides |
| [ADMIN_MANUAL.md](docs/ADMIN_MANUAL.md) | Institution setup and curriculum building |
| [ROADMAP.md](docs/ROADMAP.md) | **What is complete versus scaffolded** |

---

## Technology

React 19 · TypeScript · Tailwind CSS 4 · React Query · Dexie · Recharts ·
FastAPI · SQLAlchemy 2 · Alembic · PostgreSQL / Supabase · Docker · Vercel

**126 tests** covering the engines, API contracts, security boundaries and upload
defences. CI runs them against SQLite *and* PostgreSQL, proves the migration round
trip, and fails on model drift.

---

## Status

The training, logbook, assessment, analytics, promotion, rotation, research,
accreditation and branding paths are complete and tested end to end.

CBT delivery, PDF/DOCX rendering, notification delivery, object-storage upload,
WebSockets and SSO have schema and API surface but no implementation behind them.
[ROADMAP.md](docs/ROADMAP.md) lists each one and what is missing — read it before
demonstrating.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: [SECURITY.md](SECURITY.md) —
never a public issue.

---

<div align="center">

**Created and managed by NEXORA Technologies**

Proprietary software. © 2026 NEXORA Technologies. See [LICENSE](LICENSE).

*Manages postgraduate training records. Not a medical device. Holds no
patient-identifiable data.*

</div>
