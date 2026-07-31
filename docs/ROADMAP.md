# Build status

Written so nobody discovers a gap during a demonstration. Anything marked **Complete**
is implemented, wired end to end and exercised by the test suite or verified against a
running server.

---

## Complete

### Platform

| Area | State |
|---|---|
| Multi-tenancy | `Tenant` + 8-level `OrgUnit` tree, materialised paths, subtree queries |
| Institution branding | Logo/icon/favicon/backdrop upload with content validation, per-tenant colours, branded PWA manifest, contrast-aware ink |
| Scoped RBAC | 62 permissions, 28 roles, subtree resolution, escalation guards |
| Authentication | Argon2id, JWT with rotating refresh, TOTP MFA, recovery codes, lockout, session management |
| Audit | Append-only trail, field-level diffs, secret redaction |
| Migrations | Alembic, upgrade/downgrade round trip proven, drift check in CI |
| API | 108 routes, generated OpenAPI |
| Tests | 126 passing — engines, API contracts, security boundaries, upload defences, regression pins |

### Training

| Area | State |
|---|---|
| Curriculum builder | Specialties, programmes, versions, years, rotations, competencies/EPAs, requirement rules |
| Requirement engine | 20 measurement kinds, 6 operators, 5 scopes, windowing with interruption handling, sandboxed derived expressions |
| Rotation engine | Automatic scheduling, capacity warnings, supervisor allocation, extension with cascade, remediation, leave interruption |
| Digital logbook | Offline capture, consultant validation, bulk sign-off, query/reject with mandatory comment, locking, full audit trail |
| Assessment | User-designed instruments rendered dynamically, weighted scoring, verdict bands, entrustment ratings, competency progress |
| Academic activities | Calendar, self check-in with session codes, attendance percentages by kind, automatic CME credit posting |
| Research | Dissertation workflow, milestone gates, supervisor allocation with recorded reasoning, publications with verification |
| Analytics | 8 domain scores, RAG, immutable snapshots, trends, four role dashboards |
| Promotion engine | Requirement/time/rotation/standing gates, explainable rationale, exam eligibility, override with mandatory reason |
| Accreditation | 17 metrics, profile builder, NPMCN/WACS/WACP/MDCN/NUC profiles seeded, ranked gaps, narrative |

### Client

| Area | State |
|---|---|
| PWA shell | Manifest, hand-written service worker, install shortcuts, offline banner |
| Offline store | Dexie mirror, write outbox, conflict records |
| Sync engine | Push-then-pull, revision conflicts, auto-sync on interval/online/visibility |
| Design system | Light and dark each independently specified, validated data-viz palette, WCAG-conscious status encoding |
| Screens | 14 — sign-in, four dashboards, logbook, validation queue, rotations, competencies, academic, research, analytics, promotion, accreditation, curriculum builder, people, branding, settings |

### Deployment

Docker images for both services, Compose stack with PostgreSQL and MinIO, Nginx
configuration, GitHub Actions CI (lint, tests on SQLite *and* PostgreSQL, migration
round trip, drift check, type-check, build, bundle budget, image build, stack smoke
test), Makefile.

---

## Scaffolded — schema and API exist, no implementation behind them

Be explicit about these.

| Area | What exists | What is missing |
|---|---|---|
| **CBT item media** | `media_kind`/`media_keys` on every item; the delivery path serves them and the quality gate accepts them | Object storage for the images themselves — see the object-storage row. Blocks image, ECG, radiology and histology *authoring*, not delivery |
| **OSCE and viva** | `QuestionType.OSCE_STATION` exists; such items can be banked and reviewed | Examiner rostering to stations, station timing, per-station marksheets |
| **Report rendering** | `GeneratedReport` table, checksums, CSV export in the client | PDF/DOCX/XLSX/PPTX generation. Printable logbooks and portfolio exports are CSV or browser print today |
| **CME assignment** | Resources, assignments, credit ledger, structured articles, reading tracking and the four engagement scores | Assignment UI, resource quizzes, certificates |
| **Duty rosters** | Rosters, shifts, swaps, attendance; `duty_attendance_pct` works and the demo seeds a year of call rosters | Automatic roster generation, swap approval UI, duty scoring |
| **Multi-source feedback** | `MultiSourceFeedbackRound` with anonymity and minimum-response rules | Invitation flow, anonymous collection, aggregate release |
| **Notifications** | Rules, templates, per-user rows, `dispatch()` writes them | Delivery worker for email/SMS/push; only in-app rows are created |
| **WebSockets** | Config flag, dependency present | No handler. Dashboards poll |
| **Object storage** | S3 config, `boto3` present, `object_key` fields throughout | No upload endpoint or presigned-URL issuance for *logbook attachments and generated reports*. Branding assets do not need it - they are stored in the database by design |
| **AI assistant (conversational)** | Config flags, `ai_feedback` column on exam responses | No conversational assistant. AI *authoring* is complete — see docs/ADAPTIVE_LEARNING.md |
| **Proctoring capture** | Consent record, permission check and refusal path complete and tested | No webcam or microphone capture. The storage and retention design for biometric material is a larger piece of work than the rest of the subsystem and should not be started casually |
| **SSO / LDAP** | `TenantIntegration` holds the configuration shape | No connector |

### Adaptive learning and examinations *(new)*

Complete and tested. See `docs/ADAPTIVE_LEARNING.md`.

- Blueprint-driven paper assembly with non-repetition and reported relaxations
- Sitting lifecycle, server-enforced timer, marking, per-question feedback
- Psychometrics: KR-20, Cronbach's alpha, point-biserial, facility, SEM,
  distractor analysis, blueprint coverage
- Reading tracker and its four derived scores
- Examination Readiness Score with confidence intervals and influential factors
- Configurable examination conduct, consent-gated proctoring, post-examination
  integrity report, retention purge
- Individual learning plans generated from measured weakness
- AI generation pipeline with a twelve-check quality gate, duplicate detection
  calibrated against measured data, and an editorial gate no generated item can
  bypass
- Structured CME authoring with Vancouver and APA citations

---

## Not started

- Mobile-native wrappers (the PWA installs; no App Store presence)
- National roll-up across institutions (`platform.national.view` exists and is
  granted; no aggregation endpoint)
- Simulation and skills-lab booking
- Trainee-to-trainee peer assessment
- Timetabling beyond the rotation engine
- Payroll, HR or finance integration

---

## Known limitations

**Cohort scoring is synchronous.** `POST /analytics/score/recompute` scores up to 2,000
enrolments in one request. Fine nightly at institution scale; at national scale it
needs a task queue.

**Attendance expectation is department-level.** A trainee rotating through another
department is measured against their *home* department's mandatory sessions. Correct
for most institutions, wrong for a matrix-managed one; the `org_unit_ids` rule
parameter is the current workaround.

**Rotation planning assumes back-to-back postings.** Overlapping or part-time postings
must be entered manually. Less-than-full-time training is a real pattern and is not
modelled.

**No batching in the promotion engine.** `batch_assess` loops. At 1,000+ trainees this
wants a bulk-measurement path.

**SQLite in development, PostgreSQL in production.** CI runs the suite against both,
but the environments are not identical — notably collation, and JSON operator support
that the platform deliberately does not use.

---

## Suggested order of work

1. **Report rendering.** The most-requested missing capability. Colleges want PDFs.
2. **Object storage endpoints.** Blocks evidence upload, which blocks a real
   accreditation submission.
3. **Notification delivery worker.** The rules engine is complete and produces nothing
   anyone receives outside the app.
4. **Background worker.** The generation pipeline's twenty-minute service level
   needs one, and it is the same worker the notification delivery step wants.
5. **SSO/LDAP.** The first question every hospital IT department asks.
