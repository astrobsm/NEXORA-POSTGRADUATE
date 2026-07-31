# Security policy

Postgraduate Medical Training Console — created and managed by **NEXORA Technologies**.

Full technical detail is in [docs/SECURITY.md](docs/SECURITY.md). This file is the
reporting policy.

---

## Reporting a vulnerability

**Do not open a public issue.**

Report privately through GitHub's advisory flow:

> Repository → **Security** → **Report a vulnerability**

Include:

- what you found, and the affected version or commit;
- steps to reproduce, ideally a minimal case;
- what an attacker could achieve with it.

**Acknowledgement within two working days.** We will tell you what we intend to do
and when, and credit you in the advisory unless you would rather we did not.

---

## What we consider in scope

The system holds no patient-identifiable data by design. What it does hold is
sensitive about *staff* — assessments, competency ratings, promotion decisions. A
leaked assessment can end a career, and the severity ranking reflects that.

**High.** Cross-tenant data access. Privilege escalation. Authentication bypass.
Reading another trainee's logbook or assessments. Tampering with a validated
record, a promotion decision or the audit trail.

**Medium.** Stored or reflected XSS. Injection of any kind. Escaping the
requirement-expression sandbox. Bypassing the branding upload validation.
Account enumeration.

**Lower, but still wanted.** Missing security headers. Rate-limit gaps at the
application layer (these are expected to be handled at the ingress — see
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)). Dependency advisories with a practical
path to exploitation.

**Out of scope.** Findings that require a compromised device or an already-privileged
account. Automated scanner output without a demonstrated impact. Social engineering.
Denial of service by volume.

---

## Known gaps

Stated so nobody reports them as discoveries — they are documented deliberately in
[docs/SECURITY.md](docs/SECURITY.md#not-yet-implemented):

- No application-layer rate limiting; enforce it at the ingress.
- No field-level encryption at rest; rely on database and volume encryption.
- No SSO/LDAP connector.
- Browser storage for offline data is not encrypted at rest; sign-out clears it.

---

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | Yes |
| < 1.0 | No |

---

© NEXORA Technologies.
