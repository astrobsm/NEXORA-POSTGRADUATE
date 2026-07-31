# Administrator manual

Setting up an institution, building curricula, and running the platform. See
[CONFIGURATION.md](CONFIGURATION.md) for the full reference behind the choices here.

---

## Standing up a new institution

Roughly a day's work for a first department, and most of it is deciding policy rather
than entering it.

### 1. Reference data

```bash
python -m app.db.seed --no-demo
```

Loads 62 permissions, 28 roles, 111 specialties and the NPMCN, WACS, WACP, MDCN and
NUC accreditation profiles. No demo institution.

### 2. The institution

Create the `Tenant`, then set `settings` and `branding` (`PATCH /tenancy/tenants/current`):

```json
{
  "name": "Federal Medical Centre, Example",
  "accrediting_bodies": ["npmcn", "mdcn"],
  "branding": { "primary": "#0f766e", "accent": "#0369a1", "logo_text": "FMC" },
  "settings": {
    "academic_year_start_month": 7,
    "logbook_validation_sla_days": 7,
    "minimum_academic_attendance_percent": 75,
    "duty_hours_cap_per_week": 72,
    "promotion_committee_quorum": 5,
    "allow_self_checkin": true
  }
}
```

### 3. Branding

**Administration -> Branding.** Upload the crest and app icon, and set the two colours
everything else derives from. The preview is live - the page you are editing recolours
as you move the picker.

* **Logo** - wide wordmark or crest, for the sidebar and the sign-in screen.
* **App icon** - must be square; installed apps crop to a square and a wide logo loses
  its edges, so a non-square upload is refused with the dimensions it found.
* **Browser tab icon** - optional, falls back to the app icon.
* **Sign-in backdrop** - optional photograph behind the sign-in panel.

PNG, JPEG, WebP, GIF or SVG, up to 512 KiB. Files are validated against their actual
contents, not the extension, and an SVG carrying scripting is rejected.

Only institution leadership can change branding - CMD, Medical Director, CMAC,
Director of Residency Training, Dean, College Administrator. A head of department
cannot.

> Status colours (red / amber / green) are fixed and never follow your brand. A
> clinical signal has to mean the same thing in every institution, so the brand green
> is deliberately stepped away from the status green.

### 4. The organisational hierarchy

Hospital → Faculty → Department → Unit. Use only the levels you need; a single
department pilot is two.

Record `capacity` on each unit as you go — beds, theatres, ICU beds, library seats,
skills-lab stations. **Accreditation returns read these figures directly**, and
gathering them the week before a college visit is how institutions end up guessing.

```json
{
  "kind": "department", "name": "Department of Surgery", "code": "DEPT-SURG",
  "capacity": {
    "beds": 120, "operating_theatres": 4, "icu_beds": 6,
    "clinics_per_week": 8, "library_seats": 24, "skills_lab_stations": 4
  }
}
```

### 5. People

Create accounts and assign roles **scoped to an org unit**. A consultant assigned at
Department level supervises throughout its units; assigned at a unit, only there.

For anyone who will supervise, complete their supervisor profile — expertise,
methodologies, maximum supervisees, maximum clinical trainees, declared conflicts of
interest. The allocator uses all of it, and an incomplete profile produces poor
matches.

### 6. Programmes and curricula

Create the programme, then a curriculum version, then years, rotations, competencies
and requirement rules. Publishing supersedes the previous version.

> Trainees already enrolled stay pinned to the version they started under. Publishing
> never changes anyone's requirements retroactively.

---

## Building a curriculum

Work in this order. Each step depends on the one before.

### Training years and rotations

One year per stage. For each, add its rotations with duration in weeks, host unit and
`max_trainees`. Capacity is what makes the planner's warnings meaningful.

### Competencies and EPAs

Mark the genuinely entrustable activities as EPAs. Set `target_by_year`:

```json
{
  "code": "SURG-EPA-02",
  "title": "Perform an appendicectomy",
  "domain": "procedural_skill",
  "is_epa": true,
  "target_by_year": {
    "1": "1_observe_only",
    "2": "2_direct_supervision",
    "3": "3_indirect_supervision",
    "4": "4_independent"
  },
  "exit_target": "4_independent"
}
```

### Requirement rules

The important step. Each rule is one measurable statement — see
[CONFIGURATION.md](CONFIGURATION.md) for every available measurement.

Practical advice from building the demo curricula:

- **Write the label as a trainee would read it.** "40 major operations assisted or
  performed in year 2", not "PROC_Y2_40".
- **Record `source_reference`.** When a college revises its guidance you need to find
  every rule implementing the old version.
- **Use `mandatory` sparingly.** Everything mandatory blocks promotion. A curriculum
  where nobody can be promoted is a curriculum nobody trusts.
- **Prefer `min` aggregation on EPAs.** "Every EPA at indirect supervision" is what
  colleges mean. A mean lets a strong EPA hide a weak one.
- **Set `score_domain`.** A rule without one is evaluated but contributes to no score,
  which is rarely intended.
- **Check the totals.** Score weights are normalised, but if you weight research at
  0.4 you have said research is 40% of a trainee's standing.

### Assessment instruments

Design them in **Curriculum → instruments**. `scale` and `numeric` fields contribute
at their weight; text fields do not. Set `pass_mark` and `verdict_bands` explicitly —
the defaults are a starting point, not your institution's policy.

### Publish

Publishing requires at least one training year. It supersedes the previous active
version and stamps the approver and date.

---

## Roles and permissions

The 28 shipped roles cover the standard hierarchy. Create your own where your
institution differs — a Clinical Audit Lead, say:

```json
{
  "code": "audit_lead", "name": "Clinical Audit Lead", "rank": 40,
  "scope_kind": "department",
  "permission_codes": ["logbook.entry.read.any", "analytics.department.read"]
}
```

Two rules you cannot work around, both deliberate:

1. **You cannot grant a permission you do not hold.**
2. **You cannot assign a role more senior than your own** (lower rank is more senior).

Require MFA for every leadership and administrative role. The blast radius of a
compromised Director of Residency account is every trainee's assessments.

---

## Running the platform

### Daily

**Validation queue health.** `GET /analytics/dashboard/department` reports pending
entries. A queue older than your SLA means trainees are blocked on supervisors — chase
the supervisors, not the trainees.

**Rotation statuses.** `POST /training/maintenance/refresh-rotation-statuses` activates
rotations that have started and reports those past their end date awaiting sign-off.

### Nightly

**Recompute scorecards.** `POST /analytics/score/recompute`. Dashboards read the
denormalised score for cohort lists; without this they go stale. Detail views always
recompute live.

### Termly

**Promotion reviews.** `GET /analytics/promotion/cohort` gives the whole cohort with
each trainee's blocking requirements. Open a review per trainee, take it to committee,
record the decision. An outcome contradicting the engine requires a reason.

**Accreditation dry run.** Generate a return against your body's profile whether or
not a visit is due. The gap list is a work plan, and gaps found six months out are
fixable.

### Annually

Review requirement rules against current college guidance. Archive completed
enrolments. Confirm your backup restore actually works.

---

## Accreditation

**Accreditation → generate a return.** Choose department, standard and period.

The output is every criterion with its measured value, compliance over **essential**
criteria only, a ranked gap list and a narrative for a covering letter.

Two things to know:

**The seeded targets are illustrative.** Reconcile them against the body's current
published standard before submitting anything. Edit the profile's criteria to match.

**Figures come from validated records only.** If your logbook validation rate is poor,
your accreditation numbers will understate what your department actually does. That is
the correct behaviour — an inspector would reach the same conclusion — but it means
validation discipline is an accreditation issue, not just an administrative one.

To support a new body or a revised standard, create a profile and attach criteria
against the metric vocabulary from `GET /accreditation/metrics`. No code change.

---

## Notifications

Rules decide who hears about an event, through which channel, and how far ahead.
Templates supply the wording, with `{{token}}` substitution.

> Only in-app notifications are delivered today. Email, SMS and push require the
> delivery worker described in [ROADMAP.md](ROADMAP.md). Rules configured for those
> channels are created but not sent.

---

## Troubleshooting

**"Programme has no active curriculum version."**
Publish a version before enrolling anyone.

**"Curriculum defines no training years."**
Add at least one training year before generating a rotation schedule.

**A trainee's requirements all read zero.**
Check their enrolment is pinned to the version whose rules you edited — an enrolment
started under an older version is measured against that older version.

**Attendance shows 0% and everyone attended.**
The denominator is *mandatory* sessions in the trainee's department. If sessions were
created as non-mandatory, or under a different org unit, they are not counted.

**A year-scoped requirement measures almost nothing.**
Its window is that year of the enrolment. A trainee two months into year 3 has a
two-month window. This is correct; if you meant "across the programme", change the
scope.

**Accreditation shows a criterion in error.**
`detail.error` names the problem — usually an unknown metric after hand-editing, or a
`capacity_key` no org unit declares.

**The rule builder rejects my parameters.**
They must be valid JSON. `GET /curriculum/requirement-kinds` lists the parameters each
measurement accepts.
