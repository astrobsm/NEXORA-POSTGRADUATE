# Deployment

## Before anything else

Three things will bite you if you skip them:

1. **Set `RTC_SECRET_KEY`.** Sessions are signed with it. The default is a literal
   placeholder. `python -c "import secrets; print(secrets.token_urlsafe(48))"`
2. **Set `RTC_ENV=production`.** This makes the seeder refuse to run and enables HSTS.
3. **Set `RTC_ALLOW_DEMO_SEED=false`.** The demo institution has accounts with a
   published password.

---

## Local evaluation — no infrastructure

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate    # or source .venv/bin/activate
pip install -r requirements.txt
python -m app.db.seed
uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend
npm install
npm run dev
```

http://localhost:5173 — SQLite, no database server, no object storage.

---

## Docker Compose

```bash
cp .env.example .env
# edit RTC_SECRET_KEY at minimum
docker compose up --build
```

http://localhost:8080. Brings up PostgreSQL 16, MinIO, the API and Nginx serving the
PWA. Migrations run on container start; seeding is controlled by `RTC_SEED_ON_START`.

For a production-shaped compose deployment:

```yaml
api:
  environment:
    RTC_ENV: production
    RTC_SEED_ON_START: 'false'
    RTC_ALLOW_DEMO_SEED: 'false'
    RTC_SECRET_KEY: ${RTC_SECRET_KEY}   # from the host's secret store, not a file
web:
  build:
    args:
      SHOW_DEMO_ACCOUNTS: 'false'
```

---

## Kubernetes

A single-institution deployment is small: two Deployments, a managed database, and an
Ingress.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: rtc-api
spec:
  replicas: 3
  selector:
    matchLabels: { app: rtc-api }
  template:
    metadata:
      labels: { app: rtc-api }
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        fsGroup: 10001
      containers:
        - name: api
          image: registry.example/rtc-api:1.0.0
          ports: [{ containerPort: 8000 }]
          env:
            - name: RTC_ENV
              value: production
            # Migrations run in an initContainer, not here — see below.
            - name: RTC_RUN_MIGRATIONS
              value: 'false'
            - name: RTC_DATABASE_URL
              valueFrom: { secretKeyRef: { name: rtc-secrets, key: database-url } }
            - name: RTC_SECRET_KEY
              valueFrom: { secretKeyRef: { name: rtc-secrets, key: secret-key } }
          resources:
            requests: { cpu: 250m, memory: 512Mi }
            limits:   { cpu: '1',  memory: 1Gi }
          livenessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 20
            periodSeconds: 30
          readinessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 5
            periodSeconds: 10
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ['ALL'] }
```

### Migrations

Run them **once per release**, not once per replica. Three replicas racing
`alembic upgrade head` is how a deploy corrupts a schema.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: rtc-migrate-1-0-0
spec:
  backoffLimit: 2
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: registry.example/rtc-api:1.0.0
          command: ['alembic', 'upgrade', 'head']
          env:
            - name: RTC_DATABASE_URL
              valueFrom: { secretKeyRef: { name: rtc-secrets, key: database-url } }
```

Gate the rollout on the Job succeeding — an Argo sync hook, a Helm `pre-upgrade`
hook, or an `initContainer` that waits on it.

### Ingress

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: rtc
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: 32m
    nginx.ingress.kubernetes.io/proxy-read-timeout: '120'
    # The application enforces no rate limits; do it here.
    nginx.ingress.kubernetes.io/limit-rps: '20'
    cert-manager.io/cluster-issuer: letsencrypt-prod
spec:
  tls:
    - hosts: [rtc.hospital.example]
      secretName: rtc-tls
  rules:
    - host: rtc.hospital.example
      http:
        paths:
          - path: /api
            pathType: Prefix
            backend: { service: { name: rtc-api, port: { number: 8000 } } }
          - path: /
            pathType: Prefix
            backend: { service: { name: rtc-web, port: { number: 80 } } }
```

Keep the app and the API on **one hostname**. The service worker's cache scope, the
CORS surface and the cookie scope all collapse to one — simpler and safer than
splitting them.

---

## Sizing

Per active trainee, per year, roughly: 600–1,500 logbook entries, 20–40 assessments,
150–300 attendance records, 1 dissertation.

| Institution | Trainees | API | Database | Storage/year |
|---|---|---|---|---|
| Single department | < 50 | 1 × 0.5 vCPU / 512 MB | 2 vCPU / 4 GB | ~5 GB |
| Teaching hospital | 200–800 | 3 × 1 vCPU / 1 GB | 4 vCPU / 16 GB | ~40 GB |
| Multi-site college | 2,000–10,000 | 6+ × 2 vCPU / 2 GB | 8 vCPU / 32 GB + replica | ~300 GB |
| National | 10,000+ | Autoscaled | Partitioned + read replicas | 1 TB+ |

The API is stateless — scale horizontally. Sessions are JWTs; there is no sticky
routing.

### The queries that will hurt first

- Cohort scoring recomputes requirements per trainee. Run it nightly
  (`POST /analytics/score/recompute`), not on page load.
- Logbook listing over a four-year enrolment. The
  `(enrolment_id, occurred_at)` index carries it; keep `page_size` bounded.
- Accreditation returns aggregate a department-year. Cache the persisted
  `AccreditationReview` rather than regenerating for every viewer.

---

## Scheduled work

| Job | Cadence | Command |
|---|---|---|
| Recompute scorecards | Nightly, off-peak | `POST /analytics/score/recompute` |
| Advance rotation statuses | Daily | `POST /training/maintenance/refresh-rotation-statuses` |
| Dispatch due notifications | Hourly | Worker draining `delivery_status = 'pending'` |
| Database backup | Every 6 h | `pg_dump` to object storage |
| Audit archival | Monthly | Move `audit_logs` older than 24 months to cold storage |

---

## Backup and recovery

**Targets:** RPO 1 hour, RTO 4 hours. Justification: a lost hour costs a shift's
logbook entries, which trainees can re-enter; a lost day costs a ward round's worth of
sign-offs across an institution.

```bash
# Continuous archiving is what actually meets a 1-hour RPO.
# postgresql.conf
#   wal_level = replica
#   archive_mode = on
#   archive_command = 'aws s3 cp %p s3://rtc-wal/%f'

# Nightly base backup
pg_dump --format=custom --compress=9 "$RTC_DATABASE_URL" \
  | aws s3 cp - "s3://rtc-backups/rtc-$(date +%F).dump"

# Restore
pg_restore --clean --if-exists --no-owner -d "$RTC_DATABASE_URL" rtc-2026-07-31.dump
```

Object storage holds attachments, evidence uploads and generated reports — replicate
it to a second region. Losing it does not lose training records, but it does lose the
evidence attached to them.

**Test the restore quarterly.** An untested backup is a hypothesis.

---

## Observability

`GET /health` returns `{status, version, environment, database, time}` and reports
`degraded` when the database is unreachable — wire it to both probes.

Every response carries `X-Request-Id`, and the same id appears in the log line and in
`audit_logs.request_id`. Requests over two seconds log a warning.

Worth alerting on:

| Signal | Why |
|---|---|
| 5xx rate > 1% over 5 min | Something is broken |
| p95 latency > 2 s | Usually the cohort-scoring query |
| Failed logins > 50/min | Credential stuffing |
| `sync_conflicts` unresolved and rising | Devices losing work |
| Oldest pending validation > SLA | Trainees blocked on supervisors |
| Database connections > 80% of pool | Ahead of exhaustion |

---

## Upgrades

1. Read the release notes for schema changes.
2. Back up. Verify the backup is readable.
3. Run the migration Job. It is transactional on PostgreSQL.
4. Roll the API — the deployment is designed for one release of schema skew, so a
   rolling update is safe.
5. Roll the web image.
6. The service worker is served `no-cache`, so clients pick up the new build on their
   next navigation rather than being pinned to yesterday's bundle.

### Rollback

`alembic downgrade -1` is generated for every migration and CI proves the round trip.
A migration that drops a column cannot restore its data on downgrade — restore from
backup for those, and the release notes will say which they are.

---

## Hardening checklist

- [ ] `RTC_SECRET_KEY` from a secret manager, not a file
- [ ] `RTC_ENV=production`, `RTC_ALLOW_DEMO_SEED=false`
- [ ] TLS terminated ahead of the app; HTTP redirects to HTTPS
- [ ] `RTC_CORS_ORIGINS` set to your real origins only
- [ ] Database on a private network, TLS enforced, non-superuser role
- [ ] Rate limiting at the ingress
- [ ] Containers run as non-root with a read-only root filesystem
- [ ] Backups automated **and a restore tested**
- [ ] MFA required for every leadership and administrative role
- [ ] Log shipping to a retained store; audit archival configured
- [ ] Image scanning in CI
