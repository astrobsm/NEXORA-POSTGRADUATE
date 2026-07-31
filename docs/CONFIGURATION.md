# Configuration — how policy changes without code

This is the document to read if you are setting up an institution, or wondering
whether RTC can express your college's rules. Almost always the answer is yes, and
the mechanism is here.

---

## The requirement rule

Every "a trainee must…" statement in RTC is a row:

```jsonc
{
  "label": "40 major operations assisted or performed in year 2",
  "kind": "procedure_role_count",       // what to measure
  "operator": "gte",                    // how to compare
  "target_value": 40,                   // against what
  "scope": "training_year",             // over what period
  "severity": "mandatory",              // does it block promotion?
  "score_domain": "clinical_competency",// which score it feeds
  "weight": 2.0,                        // how much, within that domain
  "parameters": {                       // measurement-specific detail
    "roles": ["assisted", "performed_supervised", "performed_independent"],
    "grade": "major",
    "training_year": 2
  },
  "guidance": "Only entries validated by a consultant are counted.",
  "source_reference": "WACS Faculty of Surgery — operative experience guidance"
}
```

Create it in the curriculum builder, or `POST /curriculum/versions/{id}/requirements`.
It takes effect on the next evaluation — which for a trainee's dashboard is the next
page load.

`source_reference` is not decoration. When a college revises its guidance, it is how
you find every rule that implemented the old version.

---

## What can be measured

`GET /curriculum/requirement-kinds` returns this list live, with the parameters each
one accepts. Twenty measurements are implemented:

### Clinical activity

| Kind | Measures | Key parameters |
|---|---|---|
| `procedure_count` | Procedures performed | `grade`, `entry_types`, `procedure_ids`, `complexities` |
| `procedure_role_count` | Procedures at a participation role | `role`/`roles`, `grade`, `weighted` |
| `logbook_entry_count` | Any logbook entry type | `entry_types`, `org_unit_ids` |
| `clinic_count` | Outpatient clinic sessions | — |
| `ward_round_count` | Ward rounds | — |

`weighted: true` scores by competence rather than headcount — observing counts 0.25,
assisting 0.5, performing under supervision 1.0, independently 1.25, supervising
another 1.5. Closer to what a college actually means by "experience".

### Competence

| Kind | Measures | Key parameters |
|---|---|---|
| `competency_level` | Entrustment level attained | `competency_codes`, `domains`, `level`, `aggregate` |
| `epa_level` | As above, EPAs only | `level`, `aggregate` |

`aggregate` selects the statistic:
- `min` (default) — *every* competency must reach the target. Strict, and usually right.
- `mean` — average level across the set.
- `percent_at_target` — proportion at or above a level, with `level_value`.

An unrated competency scores 0 and never passes. Silence is not attainment.

### Attendance and academic engagement

| Kind | Measures | Key parameters |
|---|---|---|
| `academic_attendance_pct` | % of mandatory sessions attended | `activity_kinds`, `mandatory_only`, `org_unit_ids` |
| `duty_attendance_pct` | % of rostered duties attended | `duty_kinds` |
| `activity_presentation_count` | Presentations given | `activity_kinds`, `roles`, `include_conferences` |

The denominator is *sessions actually held* in the trainee's department during the
window — not a fixed expectation. A department that cancels half its journal clubs
does not thereby fail its trainees.

### Assessment and examination

| Kind | Measures | Key parameters |
|---|---|---|
| `assessment_pass_count` | Passed workplace assessments | `assessment_kinds`, `template_codes` |
| `assessment_mean_score` | Mean percentage score | `assessment_kinds`, `template_codes` |
| `exam_pass` | Examination passes | `paper_ids` |

### Research

| Kind | Measures | Key parameters |
|---|---|---|
| `research_output` | Projects past a stage | `min_stage`, `research_types` |
| `publication_count` | Verified publications | `publication_types`, `indexed_in`, `peer_reviewed_only`, `max_author_position` |
| `dissertation_stage` | Furthest stage reached | `stage` |

Only *verified* publications count. Self-reported output is not evidence.

### Other

| Kind | Measures | Key parameters |
|---|---|---|
| `cme_credits` | Credits from the ledger | `recognised_by` |
| `rotation_completion` | % of rotations closed as completed | `training_year` |
| `teaching_hours` | Validated teaching delivered | — |
| `custom_expression` | Arithmetic over other measurements | `expression`, `inputs` |

---

## Derived requirements

`custom_expression` composes measurements without new code:

```jsonc
{
  "label": "At least 25% of major cases performed independently",
  "kind": "custom_expression",
  "operator": "gte",
  "target_value": 25,
  "parameters": {
    "expression": "independent / total * 100",
    "inputs": {
      "independent": {
        "kind": "procedure_role_count",
        "parameters": { "role": "performed_independent", "grade": "major" }
      },
      "total": { "kind": "procedure_count", "parameters": { "grade": "major" } }
    }
  }
}
```

The evaluator accepts arithmetic over the declared inputs and nothing else — no
attribute access, no calls, no comprehensions, no names you did not declare. Division
by zero yields 0 rather than an error, so an early-career trainee with no denominator
does not break their own dashboard. `tests/test_requirements_engine.py` includes
sandbox-escape attempts.

---

## Scopes

| Scope | Window |
|---|---|
| `rotation` | The rotation's own dates |
| `training_year` | That year of the enrolment, shifted by approved interruptions |
| `programme` | Enrolment start to today |
| `promotion` | Whole enrolment; evaluated by the promotion engine |
| `exam_eligibility` | Whole enrolment; evaluated for college exam entry |

Interruption handling matters: a trainee who took six months of maternity leave in
year 2 has year 3's window shifted, so a year-scoped requirement is not assessed
against a period they were not in post.

---

## Severity

| Severity | Effect |
|---|---|
| `mandatory` | Blocks promotion. Appears in `blocking_requirements`. |
| `recommended` | Scored and shown as a gap, but never blocks. |
| `informational` | Tracked and displayed only. |

---

## Score weights

Per curriculum version, in `CurriculumVersion.score_weights`:

```json
{
  "clinical_competency": 0.30,
  "academic": 0.13,
  "attendance": 0.12,
  "research": 0.15,
  "professionalism": 0.12,
  "teaching": 0.07,
  "leadership": 0.04,
  "exam_readiness": 0.07,
  "_rag_thresholds": { "green": 75, "amber": 55 }
}
```

Weights are normalised to sum to 1.0, so a misconfigured set cannot inflate or deflate
every trainee's score. Domains with no requirements and no other evidence are
**excluded** from the overall score, not counted as zero, and the remaining weights
are renormalised.

---

## Assessment instruments

`AssessmentTemplate.form_schema` is a declarative field list the client renders
dynamically. A department invents a new instrument with no frontend change:

```json
[
  { "key": "history", "label": "History taking", "type": "scale",
    "min": 1, "max": 9, "weight": 1.0, "required": true,
    "anchors": { "1": "Well below expectation", "9": "Outstanding" } },
  { "key": "overall", "label": "Overall clinical care", "type": "scale",
    "min": 1, "max": 9, "weight": 2.0 },
  { "key": "comment", "label": "Narrative feedback", "type": "textarea" }
]
```

Field types: `scale`, `numeric`, `text`, `textarea`, `select` (with `options`),
`checkbox`, `date`. Only `scale` and `numeric` contribute to the score, at their
`weight`. A field left blank or marked not-applicable is excluded from **both**
numerator and denominator, so a partially-relevant encounter is not penalised.

Scoring configuration:

```json
{
  "method": "weighted_mean",
  "scale_max": 9,
  "pass_mark": 55,
  "verdict_bands": {
    "below_expectation": 0, "borderline": 45,
    "meets_expectation": 60, "above_expectation": 78, "outstanding": 90
  }
}
```

---

## Accreditation standards

An accrediting body's standard is an `AccreditationProfile` plus
`AccreditationCriterion` rows, expressed against the metric vocabulary from
`GET /accreditation/metrics`:

`consultant_count`, `trainer_count`, `trainee_count`, `trainer_trainee_ratio`,
`annual_procedures`, `annual_major_operations`, `annual_admissions`,
`annual_clinic_attendances`, `academic_activity_frequency`, `research_output`,
`publication_count`, `trainee_publication_rate`, `assessment_completion_rate`,
`logbook_validation_rate`, `exam_pass_rate`, `programme_count`, `infrastructure`.

```jsonc
{
  "section": "Clinical volume",
  "code": "C1",
  "title": "Annual major operations",
  "metric": "annual_major_operations",
  "operator": "gte",
  "target_value": 400,
  "unit": "procedures/year",
  "weighting": "essential",           // essential | desirable | informational
  "evidence_guidance": "Theatre register extract for the review period."
}
```

`infrastructure` reads a declared figure from `OrgUnit.capacity` via
`parameters.capacity_key` — `operating_theatres`, `icu_beds`, `library_seats`,
`skills_lab_stations`, or any key your institution records.

NPMCN, WACS, WACP, MDCN and NUC profiles ship seeded. **Their targets are
illustrative defaults** — reconcile them against the body's current published
standard before submitting anything.

> Only `essential` criteria count toward the compliance percentage. Green is ≥95%,
> amber ≥75%, red below.

---

## Institution settings

`Tenant.settings` — free-form, editable through the admin console:

| Key | Meaning |
|---|---|
| `academic_year_start_month` | 1–12 |
| `logbook_validation_sla_days` | When the validation queue flags an entry as overdue |
| `minimum_academic_attendance_percent` | Institutional floor, above any college minimum |
| `duty_hours_cap_per_week` | Used by the roster generator |
| `promotion_committee_quorum` | Members required to ratify a promotion |
| `allow_self_checkin` | Whether trainees may self-record academic attendance |
| `geo_fence_metres` | Radius for geo-verified check-in |
| `dissertation_stages` | Overrides the default dissertation workflow |
| `allocation_weights` | Overrides supervisor-matching weights |

### Branding

`Tenant.branding` holds the colours; **Administration -> Branding** in the app is the
screen for all of it.

| Key | Meaning |
|---|---|
| `primary` | Navigation, primary buttons, single-series charts |
| `accent` | Highlights and secondary emphasis |
| `logo_text` | Two to six letters, used where the full logo will not fit |
| `motto` | Optional line on the sign-in screen |

Logo, app icon, browser tab icon and sign-in backdrop are uploaded as files, stored
per institution, and served from
`/api/v1/tenancy/tenants/{tenant_id}/branding/{kind}`.

Three things worth knowing:

**Text colour is measured, not assumed.** An institution uploads the colour from its
letterhead without checking contrast. The client computes the contrast ratio of white
and near-black against the chosen colour and picks the readable one, so a pale gold
brand produces an ugly button rather than an illegible one.

**Uploads are checked against their contents.** The declared content type is a hint;
the real format is confirmed from the file's magic bytes, because browsers sniff
content regardless of what a server declares. An SVG carrying `<script>`,
`<foreignObject>`, an event handler or an external entity is rejected outright rather
than sanitised - silently altering an institution's crest is worse than asking for a
clean file. Assets are served with `nosniff` and a sandbox CSP, and the client renders
them only through `<img>`, where SVG scripting does not execute.

**Status colours never follow the brand.** Red, amber and green mean the same thing in
every institution. A hospital whose brand *is* green gets a brand green deliberately
stepped away from the status green - far enough apart that a "Validate" button never
reads as an "On track" badge - and every RAG indicator carries an icon and a text
label so the meaning never rests on hue alone.

Serving is deliberately unauthenticated: the sign-in screen, the browser tab icon and
the PWA manifest all need the logo before a session exists, and a crest is public by
nature. `GET /tenancy/public/branding` returns name, colours and asset URLs and
nothing else.

An institution-branded manifest is available at
`/api/v1/tenancy/tenants/{tenant_id}/manifest.webmanifest`, so an installed app
appears on the home screen as the hospital's rather than ours.

---

## Roles

The 28 shipped roles cover the specification's hierarchy. Institutions create their
own via `POST /users/roles`, subject to two guards:

1. **You cannot grant a permission you do not hold.** Otherwise role creation is a
   privilege-escalation path.
2. **You cannot assign a role more senior than your own** (lower `rank` is more
   senior). Otherwise a coordinator could appoint themselves CMD.

`GET /users/roles/permissions` returns the full permission vocabulary, grouped.

---

## Notifications

`NotificationRule` decides who hears about an event, through which channel, and how
far ahead:

```json
{
  "event_code": "research.milestone_due",
  "name": "Dissertation milestone reminder",
  "audience": ["trainee", "supervisor"],
  "channels": ["in_app", "email"],
  "lead_days": 14,
  "repeat_every_days": 7,
  "priority": "high",
  "conditions": { "training_years": [3, 4] }
}
```

`NotificationTemplate` supplies the wording per event and channel, with `{{token}}`
substitution. A template with a NULL tenant is the platform default; an institution's
own template overrides it.

---

## Adding a genuinely new measurement

The one case that needs code. It is small:

1. Add a member to `RequirementKind` in `app/models/enums.py`.
2. Write `measure_<kind>(ctx, rule, window) -> Measurement` in
   `app/services/requirements.py`.
3. Register it in `MEASURERS`.
4. Add its parameters to the hint map in `curriculum.requirement_vocabulary`.

`test_every_declared_kind_has_a_measurer` fails if you do step 1 and forget step 3 —
so an unmeasurable kind can never reach the rule builder's dropdown.
