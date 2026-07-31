# Security

## What this system holds

Training records for doctors and dentists: logbook entries, assessments, competency
ratings, promotion decisions, research progress.

**It does not hold patient-identifiable data.** Logbook entries carry a pseudonymous
institution-local token, an age and a sex — no name, no hospital number, no date of
birth. That is a deliberate boundary: it keeps RTC outside the scope of patient-record
regulation and removes the highest-value target from the database.

The sensitive data it *does* hold is about **staff**: performance, assessments,
promotion outcomes. A leaked assessment can end a career. The controls below are
sized for that.

---

## Authentication

| Control | Implementation |
|---|---|
| Password hashing | Argon2id — 3 iterations, 64 MiB memory, 2 lanes |
| Password policy | ≥12 characters with upper, lower, digit and symbol (length configurable) |
| Automatic rehash | On sign-in, when parameters change |
| Lockout | 6 failures → 15 minutes (both configurable) |
| MFA | TOTP with single-use recovery codes |
| Sessions | JWT: 30-minute access, 14-day refresh |
| Refresh rotation | Every refresh revokes the token used — a stolen one is single-use |
| Revocation | Per-device, from the account screen |
| Password change | Revokes every other session |

### No account enumeration

An unknown email and a wrong password return byte-identical responses. Sign-in is
otherwise a free directory of a hospital's medical staff.

---

## Authorisation

### Scoped RBAC

A `RoleAssignment` binds (user, role, org unit). A permission held at a node applies
throughout its subtree and nowhere else — a consultant in Surgery cannot read
Paediatric logbooks.

`build_principal()` resolves this once per request into `{permission → {org unit
ids}}`. `Principal.require(perm, org_unit_id=…)` raises 403 with a message naming the
missing permission.

**The client's check is a usability affordance only.** The UI hides what you cannot
do; the server enforces it on every request. Removing the frontend check would change
what is displayed, never what is permitted.

### Two escalation guards

Both exist because their absence is a privilege-escalation path, and both are tested:

1. **You cannot grant a permission you do not hold.** Otherwise creating a role is a
   way to invent authority.
2. **You cannot assign a role more senior than your own.** Otherwise a departmental
   coordinator could appoint themselves Chief Medical Director.

### Separation of duties

- A trainee cannot validate their own logbook entry.
- An assessor cannot assess themselves.
- A promotion decision contradicting the engine requires a recorded reason; the write
  is refused without one.

---

## Data integrity

Beyond confidentiality, these records are **evidence** — a college may inspect them.

| Control | Why |
|---|---|
| Validated entries are locked | A signed-off logbook that can be edited afterwards is not evidence |
| Validated entries cannot be withdrawn | Same reason |
| Full logbook audit trail | Who changed what, when, and what the values were |
| Competency ratings append-only | Progression history is not rewritten |
| Score snapshots immutable | A 2026 score can still be explained in 2029 |
| Curriculum versions pinned to enrolment | Nobody's requirements change retroactively |
| Report checksums | SHA-256 on generated exports, so a college can verify a submission |

---

## Audit

`audit_logs` records actor, action, entity, field-level diff, IP, user agent, request
id, and whether the action originated from an offline replay. Never updated or deleted
by application code.

**Secrets are redacted on write**, not on read: password hashes, MFA secrets, tokens
and API keys are replaced with a marker before the row is created, so they cannot leak
through the audit trail even if it is over-exposed.

Retention: 24 months hot, then archived to cold storage.

---

## Transport and headers

Every response:

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: geolocation=(self), camera=(), microphone=()
Strict-Transport-Security: max-age=31536000; includeSubDomains   (production only)
```

Nginx adds a CSP with no `unsafe-inline` for scripts — the build emits no inline
script, so the policy can stay tight.

---

## Injection and input handling

- **SQL** — SQLAlchemy parameterises everything. No string-built queries.
- **Requirement expressions** — institution-authored formulas are parsed to an AST and
  evaluated against an allowlist of arithmetic operators and declared inputs only. No
  attribute access, no calls, no comprehensions, no undeclared names. Sandbox-escape
  attempts are in the test suite.
- **XSS** — React escapes by default; no `dangerouslySetInnerHTML` anywhere.
- **Mass assignment** — Pydantic models define exactly what is accepted; endpoints
  taking a raw `dict` apply an explicit editable-field allowlist.
- **Path traversal** — object storage keys are server-generated.
- **Uploaded branding assets** — the declared content type is not trusted; the real
  format is confirmed from magic bytes, so an HTML file renamed `.png` cannot be stored
  and served from our origin. SVG carrying `<script>`, `<foreignObject>`, an inline
  event handler or an external entity is rejected. Assets are served with `nosniff` and
  `Content-Security-Policy: sandbox`, and rendered only through `<img>`, where SVG
  scripting does not execute. Size is capped at 512 KiB.

---

## Offline data on the device

A ward computer is shared. Therefore:

- Signing out clears the local IndexedDB mirror entirely.
- The account screen shows exactly what is cached and offers a manual clear.
- The clear is blocked while writes are still queued, so a user cannot destroy their
  own unsynced work by tidying up.
- The service worker never caches authentication responses.

Browser storage is not encrypted at rest. On a genuinely shared device, rely on OS
account separation and full-disk encryption — and on the sign-out behaviour above.

---

## Regulatory posture

### NDPR (Nigeria Data Protection Regulation)

| Requirement | How |
|---|---|
| Lawful basis | Employment and professional-training obligation |
| Data minimisation | No patient identifiers; staff data limited to training relevance |
| Subject access | Every trainee can export their full portfolio |
| Rectification | Amendment with an audit trail; validated records use a documented correction path |
| Retention | Configurable; training records normally retained for professional-registration lifetime |
| Breach notification | Audit trail supports the 72-hour assessment |

### GDPR-ready

Same controls. Additionally: data is single-region by deployment; erasure requests
against training records held for professional-registration purposes fall under the
legal-obligation exemption and should be assessed case by case with your DPO.

### HIPAA-inspired

RTC is not a covered system — it holds no PHI. The access-control, audit and integrity
controls follow the Security Rule's technical safeguards as a baseline because they
are good practice for staff data too.

---

## Threat model

| Threat | Control |
|---|---|
| Credential stuffing | Argon2id, lockout, MFA, no enumeration, ingress rate limiting |
| Stolen refresh token | Rotation on use; replay fails and is auditable |
| Trainee inflating their record | Nothing counts until a consultant validates it |
| Supervisor's sign-off silently overwritten | Revision-based conflict detection; never last-write-wins |
| Privilege escalation via role creation | Cannot grant unheld permissions; cannot assign a senior role |
| Cross-department data access | Subtree-scoped permission resolution on every request |
| Cross-tenant leakage | `tenant_id` filter on every read path; tested |
| Malicious requirement expression | AST allowlist; no callables reachable |
| Stored XSS via an uploaded logo | Magic-byte format check, SVG script rejection, sandbox CSP, `<img>`-only rendering |
| Data loss on a lost device | Server is authoritative; outbox replays; sign-out wipes local data |
| Insider exfiltration | Audit trail on export; per-request attribution |

---

## Reporting a vulnerability

Do not open a public issue. Contact the platform security team with the affected
version, reproduction steps and impact. Acknowledgement within two working days.

---

## Not yet implemented

Stated plainly so nobody assumes otherwise:

- **Application-layer rate limiting** — enforce at the ingress.
- **Field-level encryption at rest** — rely on database and volume encryption.
- **SSO / LDAP** — `TenantIntegration` holds the configuration shape; no connector
  is implemented.
- **Automated penetration testing in CI** — image scanning only.
