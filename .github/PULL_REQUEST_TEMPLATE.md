## What and why

<!-- What changed, and what problem it solves. The diff already lists the files. -->

## How it was verified

<!-- Not "tests pass" — what did you actually check? -->

- [ ] `make check` passes (lint, tests, type-check, build)
- [ ] New tests cover the change, and fail without it

## Deployment notes

- [ ] No schema change
- [ ] Schema change — migration included, and **Migrate database** must run before deploy
- [ ] New environment variable(s): <!-- name them -->
- [ ] Reference data must be reseeded (`python -m app.db.seed --no-demo`)

<!-- If a migration cannot restore data on downgrade, say so here. -->

## Interface changes

<!-- Screenshots in both light and dark mode, or delete this section. -->

## Checklist

- [ ] Policy is expressed as data, not as a code branch on an institution or programme
- [ ] Permission checks are enforced server-side, not only hidden in the client
- [ ] Any new permission is granted to at least one role
- [ ] Error messages are written for the clinician who will read them
- [ ] No patient-identifiable data is stored or logged

---

*Postgraduate Medical Training Console — created and managed by NEXORA Technologies.*
