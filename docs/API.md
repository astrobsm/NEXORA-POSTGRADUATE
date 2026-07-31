# API reference

Base path `/api/v1`. Interactive documentation is generated from the code and is
always current:

- **Swagger UI** — http://localhost:8000/docs
- **ReDoc** — http://localhost:8000/redoc
- **OpenAPI JSON** — http://localhost:8000/api/v1/openapi.json (or `make openapi`)

This document covers the conventions and the endpoints whose behaviour is not obvious
from their signature.

---

## Conventions

### Authentication

`Authorization: Bearer <access_token>`. Access tokens last 30 minutes; refresh tokens
14 days and **rotate on use** — a refresh token cannot be replayed.

Platform administrators additionally send `X-Tenant-Id` to act on a chosen
institution.

### Errors

```json
{
  "detail": "A comment is required when returning or rejecting an entry, so the trainee knows what to correct.",
  "code": "http_422"
}
```

Validation failures add `field_errors`. Messages are written for the person reading
them, not for a log file.

| Status | Meaning here |
|---|---|
| 400 | No institution context |
| 401 | Missing, invalid or expired token |
| 403 | Authenticated but not permitted |
| 404 | Not found, or not visible in your scope |
| 409 | Conflicts with current state (validated entry, unmet rotation requirement) |
| 422 | Payload failed validation |
| 423 | Account locked after repeated failures |

### Pagination

`?page=1&page_size=50` → `{ "items": [...], "total": 412, "page": 1, "page_size": 50 }`

### Headers

Every response carries `X-Request-Id` and `X-Response-Time-Ms`. Quote the request id
when reporting a problem — it appears in the server log and the audit trail.

---

## Endpoint groups

| Prefix | Purpose |
|---|---|
| `/auth` | Sign-in, MFA, refresh, sessions, password |
| `/meta` | Reference vocabularies, notifications |
| `/tenancy` | Institutions, the org hierarchy, and branding assets |
| `/users` | Directory, roles, assignments, supervisor profiles |
| `/curriculum` | Specialties, programmes, versions, requirement rules, procedures |
| `/training` | Enrolments, rotation engine, leave |
| `/logbook` | Capture, validation, summaries, audit trail |
| `/assessments` | Instruments, submissions, competency progress |
| `/academic` | Activities, attendance, CME ledger |
| `/research` | Projects, supervisor allocation, milestones, publications |
| `/analytics` | Scores, promotion, role dashboards |
| `/accreditation` | Profiles, criteria, generated returns |
| `/sync` | Offline pull, push, conflict resolution |

---

## Authentication

### `POST /auth/login`

```json
{ "email": "registrar1@uthdemo.health", "password": "…" }
```

Returns either a token pair or, when MFA is enabled, a challenge:

```json
{ "mfa_required": true, "challenge_token": "…" }
```

Then `POST /auth/mfa/verify` with `{ "challenge_token": …, "code": "123456" }`.
Recovery codes are accepted in place of a TOTP code and are single-use.

> The response for an unknown account is byte-identical to a wrong password, so the
> endpoint cannot enumerate staff email addresses. Six failures lock the account for
> fifteen minutes.

### `GET /auth/me`

The screen-driving call. Returns the user, their institution, every role assignment
with its scope, the resolved permission list, and the trainee's enrolment summary if
they have one.

---

## Logbook

### `POST /logbook`

Creates an entry as `pending`. **Nothing counts toward a requirement until a
consultant validates it.**

Send `client_uuid` for offline-created entries: the endpoint is idempotent on it, so
a retry after a dropped connection returns the existing entry rather than duplicating
a clinical record.

### `POST /logbook/{id}/validation`

```json
{
  "decision": "validated",
  "comment": "Good technique; consider the retrograde approach next time.",
  "competency_ratings": [
    { "competency_id": "…", "level": "3_indirect_supervision",
      "evidence": "Managed the dissection independently." }
  ]
}
```

`decision` is `validated`, `queried` or `rejected`. A comment is **required** for the
latter two — returning an entry without saying why wastes the trainee's next attempt.

Entrustment ratings can be awarded in the same action, which is how supervisors
actually work: judgement and evidence together.

Once validated, an entry is locked. The trainee cannot edit it and cannot withdraw it;
it is evidence a college may inspect.

### `POST /logbook/validation/bulk`

Body is a JSON array of entry ids. Only entries assigned to the caller are affected;
anything skipped is reported back with a reason rather than silently ignored.

---

## Training and the rotation engine

### `POST /training/enrolments/{id}/rotations/plan`

Dry run. Returns the schedule the engine *would* create, each posting's proposed
supervisor with the reasoning behind the choice, and capacity warnings where a unit
is already full for the window.

### `POST /training/enrolments/{id}/rotations/generate`

Commits the plan. `replace_planned=true` removes existing **planned** rotations only —
anything active, completed or under remediation is never destroyed by re-planning.

### `POST /training/rotations/{id}/close`

Refuses with **409** when mandatory rotation requirements are unmet. A supervisor may
override with `force: true`, which then requires a comment; the override is recorded
in the audit trail.

### `POST /training/rotations/{id}/extend`

Extends a rotation and, by default, shifts every subsequent planned rotation by the
same period. Returns every rotation it touched.

### `POST /training/leave/{id}/decision`

Approving leave marked `extends_training` adds to `interruption_days`, moves the
expected end date, and reschedules the affected rotations.

---

## Analytics and promotion

### `GET /analytics/enrolments/{id}/score`

The full scorecard, computed live. `include_rules=true` returns every requirement with
what was measured, what was required, the shortfall, and the evidence counts behind
the number. Nothing about a score is opaque to the trainee.

Note `unassessed_domains`: domains this curriculum does not measure are reported as
*not assessed* and excluded from the overall score, rather than counted as zero.

### `GET /analytics/enrolments/{id}/promotion`

Every promotion gate and its result:

```json
{
  "outcome": "not_recommended",
  "readiness_percent": 46.8,
  "rationale": "Not recommended. 18 of 24 required training months served. 7 mandatory requirement(s) unmet: …",
  "checks": {
    "requirements": { "total": 19, "met": 12, "blocking_unmet": 7, "passed": false },
    "time_served": { "months_served": 18, "months_required": 24, "interruption_days": 0, "passed": false },
    "rotations": { "planned": 4, "completed": 2, "outstanding": [...], "passed": false },
    "standing": { "status": "active", "passed": true }
  },
  "blocking": [ … ],
  "advisories": [ … ]
}
```

### `POST /analytics/promotion/reviews/{id}/decision`

A decision that contradicts the engine is accepted — committees hold information the
system does not — but **`override_reason` is required**, and the request is refused
with 422 without one. Approval advances the trainee's year and level, or completes
their programme if they were in the final year.

### Dashboards

`/analytics/dashboard/{trainee|supervisor|department|institution}` — each returns a
purpose-built payload rather than a generic aggregate, so a dashboard is one request.

---

## Accreditation

### `POST /accreditation/reviews`

```
?org_unit_id=…&profile_id=…&period_start=2025-07-31&period_end=2026-07-31&persist=true
```

Evaluates a department against a body's standard and returns every criterion with
measured value and target, the compliance percentage over **essential** criteria only,
a ranked gap list, and a plain-language narrative suitable for a covering letter.

Figures come from validated records only — the same numbers an inspector would reach.

---

## Institution branding

### `GET /tenancy/public/branding`

Unauthenticated. Returns the institution's name, colours and asset URLs so the sign-in
screen can render its identity before anyone has a session. Resolves by `?code=`, or
automatically on a single-institution deployment.

### `PUT /tenancy/tenants/current/branding/{kind}`

Multipart upload. `kind` is `logo`, `icon`, `favicon` or `login_backdrop`. Requires
`tenancy.settings.manage`.

Refuses, with a message the uploader can act on:

- a file whose real format does not match its declared type (a browser sniffs content
  regardless of what a server declares);
- an SVG carrying scripting or embedded content;
- anything over 512 KiB;
- a non-square app icon, quoting the dimensions it found.

### `GET /tenancy/tenants/{tenant_id}/branding/{kind}`

Unauthenticated, for the same reason as the public lookup. Served with an ETag, so an
unchanged crest is a 304 rather than a re-send, and with `nosniff` plus a sandbox CSP
so an uploaded SVG cannot execute even if opened directly.

### `GET /tenancy/tenants/{tenant_id}/manifest.webmanifest`

An institution-branded PWA manifest, so an installed app appears on the home screen as
the hospital's.

---

## Offline synchronisation

### `GET /sync/pull`

```
?device_id=…&since=2026-07-30T10:00:00Z&collections=log_entries&collections=enrolments
```

Returns rows changed since the cursor, scoped to what the caller may hold on a device.
The server tracks a per-device cursor, so `since` is optional after the first call.
`has_more` signals another page.

### `POST /sync/push`

```json
{
  "device_id": "…",
  "items": [
    { "collection": "log_entries", "op": "create", "client_uuid": "…", "data": { … } },
    { "collection": "log_entries", "op": "update", "id": "…", "base_revision": 3, "data": { … } }
  ]
}
```

Three outcomes per item:

- **applied** — created (idempotent on `client_uuid`) or updated.
- **conflict** — `base_revision` no longer matches the server. The server row is
  **left untouched** and both versions are returned for the user to resolve. Clinical
  records are too important for last-write-wins.
- **rejected** — permanent failure, with a reason. Only `log_entries` and
  `assessments` are writable from a device; everything else is server-authoritative.

### `POST /sync/conflicts/{id}/resolve`

`?resolution=server_wins|client_wins`.

---

## Reference data

### `GET /meta/vocabularies`

Unauthenticated, deliberately: the sign-in screen and the offline shell need it before
a session exists. Returns every enumeration the client renders — activity kinds,
entrustment levels with their ranks, participation roles with their competence
weights, requirement kinds, dissertation stages with their order, and more. The
frontend never hard-codes a domain list the backend owns.

### `GET /curriculum/requirement-kinds`

Everything the rule builder needs: which measurements exist, which are implemented,
what parameters each accepts, and the valid operators, scopes, severities and score
domains.

### `GET /accreditation/metrics`

The metric vocabulary for authoring accreditation criteria.

---

## Rate limits and quotas

Not implemented at the application layer. Deploy behind an ingress or API gateway that
enforces them — see [DEPLOYMENT.md](DEPLOYMENT.md).
