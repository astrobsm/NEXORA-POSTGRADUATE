# Deploying on Vercel with Supabase

Created and managed by **NEXORA Technologies**.

---

## Read this first

Vercel runs Python as **short-lived serverless functions**. Most of this platform
suits that well. Three parts do not, and pretending otherwise produces a
deployment that works in testing and fails in a department:

| Doesn't fit | Why | Where it goes instead |
|---|---|---|
| **Alembic migrations** | A cold deploy invokes the function concurrently; several processes racing `upgrade head` corrupts a schema | `.github/workflows/migrate.yml` |
| **Cohort score recomputation** | Iterates every active trainee against every rule — minutes, not seconds | `.github/workflows/scheduled.yml` |
| **Argon2id password hashing** | 64 MiB and ~200 ms per hash; on a cold start that eats most of a Hobby plan's 10-second budget | Works, but see [Argon2 and cold starts](#argon2-and-cold-starts) |

Everything else — the API, the requirement engine, dashboards, the offline sync
protocol — is request-scoped and runs comfortably.

**Plan requirement.** `vercel.json` sets `maxDuration: 60`, which needs **Pro**.
On Hobby the ceiling is 10 seconds; drop the value to `10` and expect the
accreditation return endpoint to time out on a large department.

> **If you would rather not accept these constraints**, the Docker Compose stack in
> [DEPLOYMENT.md](DEPLOYMENT.md) runs the identical codebase on any container host
> with none of them. Vercel is a good choice for the frontend either way.

---

## 1. Supabase

### Create the project

1. [supabase.com](https://supabase.com) → **New project**.
2. Choose the region closest to your users — for Nigeria, `eu-west-2` (London)
   usually beats any US region.
3. Set a strong database password and record it.

### Get the two connection strings

**Project Settings → Database → Connection string → URI.**

You need **both**, and using the wrong one is the most common failure here:

| Use | Port | Mode | For |
|---|---|---|---|
| Application | `6543` | Transaction | Vercel functions |
| Migrations | `5432` | Session | Alembic, scripts |

Swap the driver prefix to `postgresql+psycopg://`:

```
postgresql+psycopg://postgres.abcdefgh:PASSWORD@aws-0-eu-west-2.pooler.supabase.com:6543/postgres
postgresql+psycopg://postgres.abcdefgh:PASSWORD@aws-0-eu-west-2.pooler.supabase.com:5432/postgres
```

**Why two.** PgBouncer in transaction mode hands each transaction a different
backend connection. That is exactly what you want for hundreds of ephemeral
functions, and exactly what breaks Alembic, which runs DDL across multi-statement
transactions. The application detects port 6543 and disables prepared statements
automatically; the migration workflow refuses to run against it at all.

> **URL-encode the password** if it contains `@ : / # ? &`. A `@` in a password
> silently truncates the host and produces a baffling connection error.

### Prepare the database

```bash
git clone https://github.com/astrobsm/NEXORA-POSTGRADUATE.git
cd NEXORA-POSTGRADUATE

python -m venv backend/.venv
source backend/.venv/bin/activate          # Windows: backend\.venv\Scripts\activate
pip install -r backend/requirements.txt

export RTC_DATABASE_URL='postgresql+psycopg://...:5432/postgres'   # SESSION pooler
export RTC_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"

python scripts/supabase_bootstrap.py --reference-data
```

That applies every migration and loads 63 permissions, 28 roles, 111 specialties
and the NPMCN / WACS / WACP / MDCN / NUC accreditation standards. It refuses to
run against the transaction pooler and tells you why.

### Create your first administrator

Production never runs the demo seeder, so nothing can sign in yet:

```bash
python scripts/create_admin.py \
  --institution "Federal Medical Centre, Owerri" --code FMC-OWE \
  --email cmd@fmcowerri.gov.ng --name "Adaeze Nwachukwu" \
  --role chief_medical_director
```

### Lock it down

**Database → Network Restrictions.** Vercel does not publish stable egress IPs on
lower plans, so restricting by IP will break the app unless you are on an
enterprise plan with static egress. Rely on the connection password and TLS.

**Row Level Security.** RTC does its own tenant isolation in the application layer
and connects as the owner, so Supabase RLS is not used. Do not enable it on RTC's
tables expecting it to help — it will only break writes.

**Supabase Auth is not used.** RTC has its own authentication, scoped RBAC and
audit trail. Adding Supabase Auth would give you two conflicting answers to "who
is this user".

---

## 2. Vercel

### Import

**Add New → Project → Import** `astrobsm/NEXORA-POSTGRADUATE`.

Leave the framework preset as **Other**. `vercel.json` already declares the build
command, output directory and the Python function.

### Environment variables

**Settings → Environment Variables.** These are the minimum:

| Name | Value | Notes |
|---|---|---|
| `RTC_DATABASE_URL` | the **6543** URI | Transaction pooler |
| `RTC_SECRET_KEY` | 48+ random characters | Sessions are signed with it |
| `RTC_ENV` | `production` | Blocks the seeder, enables HSTS |
| `RTC_ALLOW_DEMO_SEED` | `false` | Belt and braces |
| `VITE_SHOW_DEMO_ACCOUNTS` | `false` | Hides the demo buttons on sign-in |

Optional:

| Name | Default | Notes |
|---|---|---|
| `RTC_ACCESS_TOKEN_TTL_MINUTES` | `30` | |
| `RTC_REFRESH_TOKEN_TTL_DAYS` | `14` | |
| `RTC_PASSWORD_MIN_LENGTH` | `12` | |
| `RTC_MAX_FAILED_LOGINS` | `6` | |
| `RTC_DB_SERVERLESS` | auto | Force the no-pool strategy if detection fails |

`RTC_CORS_ORIGINS` is **not** needed: the app and API share one origin.

> Generate the secret with
> `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
> Rotating it signs everyone out.

### Deploy

Push to `main`, or **Deploy** in the dashboard. First build takes a few minutes.

Then check:

```
https://your-app.vercel.app/health     → {"status":"ok","database":"ok",…}
https://your-app.vercel.app/docs       → API documentation
https://your-app.vercel.app/           → the sign-in screen
```

If `/health` reports `"database":"unavailable"`, it is almost always the
connection string: wrong port, or an unencoded `@` in the password.

---

## 3. GitHub Actions

`vercel.json` sets `github.silent`, so Vercel's own Git integration does not
deploy. `.github/workflows/deploy.yml` does, and only after CI passes — a red
test suite should never reach a department.

### Repository secrets

**Settings → Secrets and variables → Actions.**

| Secret | Where to get it |
|---|---|
| `VERCEL_TOKEN` | Vercel → Account Settings → Tokens |
| `VERCEL_ORG_ID` | `.vercel/project.json` after `vercel link` |
| `VERCEL_PROJECT_ID` | same file |
| `RTC_DATABASE_URL_SESSION` | the **5432** URI, for migrations |
| `RTC_SECRET_KEY` | the same value as in Vercel |
| `RTC_BASE_URL` | `https://your-app.vercel.app` |
| `RTC_SERVICE_EMAIL` | a service account for scheduled jobs |
| `RTC_SERVICE_PASSWORD` | its password |

### The service account

Nightly maintenance signs in as a real user, so it exercises the same permission
checks a human would:

```bash
python scripts/create_admin.py \
  --institution "Federal Medical Centre, Owerri" --code FMC-OWE \
  --email automation@fmcowerri.gov.ng --name "Scheduled Maintenance" \
  --role director_residency --generate-password
```

**Do not enable MFA on it** — a scheduled job cannot complete a TOTP challenge.
Compensate by giving it only the role it needs, and by reviewing its audit trail;
every action it takes is attributed to it.

### Migrations

Never automatic. Run **Actions → Migrate database → Run workflow**, choose the
environment, and type the environment name to confirm. It refuses the transaction
pooler, shows you what is pending, applies it, then verifies no drift remains.

Run it **before** deploying a release that contains a schema change.

---

## 4. Custom domain

**Settings → Domains** → add `rtc.yourhospital.gov.ng`, then create the CNAME
Vercel shows you. TLS is issued automatically.

Update `RTC_BASE_URL` in your GitHub secrets afterwards, or nightly maintenance
will keep hitting the old hostname.

---

## Argon2 and cold starts

Password hashing is configured at 64 MiB of memory and 3 iterations — deliberately
expensive, because that is what makes a stolen password database useless.

On a warm function this costs ~200 ms. On a cold start it is added to a
2–4 second Python initialisation. On Hobby's 10-second ceiling, a sign-in that
lands on a cold function can get uncomfortably close.

Options, in order of preference:

1. **Use Pro** and the 60-second ceiling. Sign-in is not latency-critical.
2. **Accept it.** Only sign-in and password change hash; every other request just
   verifies a JWT signature.
3. **Reduce the cost** only if you must, and understand what you are trading. The
   parameters are in `backend/app/core/security.py`.

Do not lower it silently to make a benchmark look better.

---

## What still doesn't work here

Be honest with whoever operates this:

**Object storage.** Logbook attachments and generated reports need S3-compatible
storage, which is not wired up on Vercel. Institution branding is unaffected — those
assets live in the database by design. See [ROADMAP.md](ROADMAP.md).

**Report rendering.** PDF/DOCX/XLSX generation is not implemented anywhere yet.
CSV export and browser print work.

**Notification delivery.** In-app notifications are written; email, SMS and push
need a worker that does not exist yet.

**WebSockets.** Vercel's Python functions do not hold connections. Dashboards poll.
Nothing in the product depends on push today.

---

## Troubleshooting

**`{"database":"unavailable"}`** — the connection string. Check the port is 6543,
the password is URL-encoded, and the region in the host matches your project.

**`prepared statement "__asyncpg_1__" already exists`** or similar** — the app is
on the session pooler, or `uses_pgbouncer` did not detect PgBouncer. Confirm the
URL contains `:6543`, or set `RTC_DB_SERVERLESS=true`.

**`remaining connection slots are reserved`** — something is holding a pool.
Confirm `RTC_DATABASE_URL` uses 6543, and that `RTC_DB_SERVERLESS` is not set to
`false`.

**Function timeout on accreditation** — a large department over a long period. On
Hobby, shorten the review period; on Pro, the 60-second budget is usually enough.

**Everyone is signed out after a deploy** — `RTC_SECRET_KEY` changed. It must be
identical across every environment and every deployment.

**`Target database is not up to date`** — pending migrations. Run the migrate
workflow.

**Sign-in works, dashboards are empty** — reference data was never loaded. Run
`python scripts/supabase_bootstrap.py --reference-data`.

---

## Cost, roughly

| | Free | Realistic |
|---|---|---|
| Vercel | Hobby: 10 s functions, non-commercial only | Pro, $20/user/month |
| Supabase | Free: 500 MB, pauses after 7 days idle | Pro, $25/month, 8 GB |

A teaching hospital with 200 trainees generates roughly 40 GB of database over
four years — see the sizing table in [DEPLOYMENT.md](DEPLOYMENT.md).

**A paused free-tier Supabase project takes the platform down.** Do not run a
department on it.

---

*Postgraduate Medical Training Console — created and managed by NEXORA Technologies.*
