# Adaptive learning, CBT, and AI-assisted authoring

*Postgraduate Medical Training Console — created and managed by NEXORA Innovations.*

This document covers the examination and learning-analytics subsystem: how a
paper is assembled and sat, how the Examination Readiness Score is computed,
what the integrity layer does and deliberately does not do, and how AI-assisted
authoring is governed.

It is written to be read by a departmental examinations officer as much as by a
developer, because most of the decisions here are policy decisions that happen
to be implemented in code.

---

## 1. What is built, and what is not

**Built and tested.**

| Capability | Where |
|---|---|
| Blueprint-driven paper assembly, non-repetition, adaptive difficulty | `app/services/cbt_engine.py` |
| Sitting lifecycle, server-enforced timer, marking, per-question feedback | `app/services/cbt_engine.py` |
| KR-20, Cronbach's alpha, point-biserial, facility, distractor analysis | `app/services/psychometrics.py` |
| Reading tracker and its four derived scores | `app/services/reading.py` |
| Examination Readiness Score, confidence intervals, influential factors | `app/services/readiness.py` |
| Examination conduct policy, event capture, post-examination report | `app/services/integrity.py` |
| Individual learning plans from measured weakness | `app/services/remediation.py` |
| Item quality gate and duplicate detection | `app/services/ai/quality.py` |
| Nine-stage generation pipeline | `app/services/ai/pipeline.py` |
| Structured CME article authoring, Vancouver and APA citations | `app/services/ai/cme_author.py` |
| Editorial workflow and the publication gate | `app/services/editorial.py` |

**Not built.** Stated plainly rather than left to be discovered:

- **Image, ECG, histology and radiology item *authoring*.** The data model
  carries `media_kind` and `media_keys`, the delivery path serves them, and the
  quality gate accepts them. What is missing is object storage for the images
  themselves — the same gap `docs/ROADMAP.md` records for every other
  attachment in the platform.
- **OSCE station and viva scheduling.** `QuestionType.OSCE_STATION` exists and
  such items can be banked and reviewed; there is no rostering of examiners to
  stations.
- **Webcam and microphone capture.** The consent record, the permission check
  and the refusal path are complete and tested. No capture code exists, because
  the storage and retention design for biometric material is a larger piece of
  work than the rest of this subsystem combined and should not be started
  casually.
- **Item response theory.** Deliberate. See §3.

---

## 2. The three things that are true regardless of configuration

Almost everything in this subsystem is configurable. These three are not,
because they are the guarantees the rest rests on.

### AI-generated content cannot reach a candidate unreviewed

Generated items are created with `editorial_status = ai_draft`. The paper
assembler filters on `editorial_status == published` and nothing else is
servable. The transition table in `app/services/editorial.py` has no edge from
`ai_draft` to `published`; the only route is
`ai_draft → in_review → approved → published`, and every transition records the
user who made it.

There is a second, redundant check in `review_question` that refuses to publish
AI-generated content with no recorded approval. It exists in case someone later
loosens the transition table without realising what it was for.

### Correct answers do not leave the server during a sitting

`serve_questions` returns a `ServedQuestion`, whose options carry only `key` and
`text`. Keys, rationales and explanations are stripped in the service layer, not
in the API layer, so no future endpoint can serialise a whole `Question` and
hand out the answers by accident. Feedback is a separate endpoint that refuses
while an attempt is in progress.

### The integrity engine cannot penalise anyone

`build_report` writes only `clean` or `pending_review`. Every other outcome
requires `record_review_decision`, which demands a named reviewer and
non-empty reasoning. There is no code path from browser telemetry to a changed
score, a voided attempt, or a misconduct record.

Observations are phrased as description. "The examination window lost focus 11
times" is a fact; "the candidate was looking things up" is a conclusion that
browser telemetry cannot support, and a test asserts that no observation
contains that kind of language.

---

## 3. Computer-based testing

### Assembly

A paper is drawn from a blueprint (proportions per curriculum category) and a
difficulty mix (proportions per band). Both default to the values the curriculum
specification fixes and both are overridable per paper.

Proportions become integer counts by largest-remainder apportionment, so a
50-item paper contains exactly 50 items. Rounding nine categories independently
routinely yields 49 or 51 — a defect that surfaces only when a candidate counts.

Where the bank cannot fill a cell, the assembler relaxes in a fixed order:
first the band within the category (a category is a curriculum promise, a band
is a calibration preference), then the category. **Every relaxation is counted
and reported.** A paper assembled by relaxing thirty of fifty cells is
technically a paper, and the department should be told.

### Non-repetition

`recent_question_ids` excludes anything the candidate has seen inside a rotation
window (12 weeks by default). It is a query over attempts rather than a stored
list, because attempts are the source of truth and a cached exclusion list goes
stale exactly when it matters.

Within a bucket, items sort: previously answered incorrectly, then on a weak
topic, then never served, then least recently served. The last is what makes a
large bank actually rotate.

### Adaptive difficulty

`adaptive_target` is a running ability estimate damped by the square root of the
evidence: the first answer moves the target by a full step, the sixteenth barely
moves it. Bounded to [0.05, 0.95] so a run of correct answers cannot walk a
candidate off the top of the bank.

**This is not item response theory, deliberately.** IRT needs calibrated item
parameters from hundreds of candidates per item. A departmental bank in its
first year has none, and fitting a three-parameter model to forty responses
produces confident nonsense. The per-item statistics this platform already
stores are exactly what a later IRT calibration would need, so this is a floor
rather than a dead end.

### Psychometrics

Every statistic returns `None` rather than a placeholder when it is undefined.
A reliability of 0.0 and an undefined reliability mean very different things to
an examinations officer.

Two details worth knowing:

- **Point-biserial is corrected against the rest score** — the total with the
  item's own contribution removed. Without the correction every item correlates
  with itself through the total, which inflates discrimination on short papers
  badly enough to make a 20-item quiz look better than a 200-item exam.
- **Reliability is computed only over items every candidate saw.** With
  randomised delivery from a large pool that can be a small subset; reporting a
  coefficient over a ragged matrix would be meaningless.

`is_defensible` reports whether reliability supports a pass/fail decision
(≥ 0.70). The SEM is reported in percentage points, which is what turns a
borderline result into an honest one: a candidate two marks below the pass mark
on a paper with an SEM of four marks has not demonstrably failed.

---

## 4. Examination readiness

Eight weighted components, exactly as the curriculum specification fixes them:

| Component | Weight |
|---|---|
| CBT performance | 35% |
| CME reading completion | 20% |
| Procedural logbook | 15% |
| Clinical competency | 10% |
| Seminar participation | 5% |
| Journal club participation | 5% |
| Case presentations | 5% |
| Professionalism / consultant evaluation | 5% |

Categories: Outstanding 90–100, Examination Ready 80–89, Nearly Ready 70–79,
Needs Improvement 60–69, Intensive Remediation below 60.

Three design decisions make the number defensible.

**Unassessed components are excluded and the weights renormalised, never scored
zero.** A department that has not yet run a journal club has produced no
evidence about its trainees' journal-club participation; recording that as 0/100
would penalise every one of them for an administrative gap. The API reports
`evidence_coverage` — the share of the weight table that had evidence behind it
— so the honest headline is always available.

**Every score carries a confidence interval.** Component uncertainty falls as
the square root of the evidence behind it and the components combine in
quadrature; missing components add their own term. The interval is *drawn* on
the readiness screen rather than hidden in a tooltip, because "82 (74–90)" and
"82 (81–83)" are different statements.

**Influential factors are computed by counterfactual, not by weight.** Asking
"which component has the largest weight?" gives the same answer for everyone.
Asking "if this trainee improved this component by ten points, how much would
the total move?" ranks a badly-lagging 5% component above a nearly-complete 35%
one when that is the truth.

### The reading tracker

Four scores, each built around the way it would otherwise be gamed:

- **Reading** rewards completion, not time. `active_seconds` counts only
  heartbeats received while the document was visible, capped at 90 seconds per
  gap, so leaving a tab open overnight earns nothing.
- **Consistency** is normalised entropy over daily active minutes. Six hours the
  night before scores zero; forty minutes on nine days scores highly. Entropy
  rather than a day count, because a count cannot distinguish nine light days
  plus one enormous one from ten even days.
- **Engagement** weights highlights, notes, references followed and so on, each
  **capped** before weighting. Without the caps the fastest route to a perfect
  score is to select every paragraph in one article.
- **Retention** is accuracy on items covering topics read at least a fortnight
  earlier. Returns `null`, not zero, below five such items — a trainee in their
  first fortnight has no retention to measure.

Reading annotations are private to their author. There is no `user_id`
parameter on the annotations endpoint and no supervisory override: a trainee's
marginal notes are study material, not evidence.

---

## 5. Examination conduct

Every measure is a column on `IntegrityPolicy`. **With no policy configured,
nothing is required and nothing is logged** — the correct default for a
formative quiz, and it takes no configuration to get.

Turning a measure off means the data is not collected. `record_event` returns
`None` and stores nothing when policy declines a class of event; storing it
anyway would breach both the institution's configuration and NDPR data
minimisation, and would be invisible until a subject access request.

### Identifiers

Device fingerprints and IP addresses are never stored. They are HMAC-hashed with
the application secret **and the tenant id**, so the same device produces
different hashes at different institutions and no cross-institution correlation
is possible even if two databases are joined. The hash still answers "did the
device change mid-examination?", which is the only question the platform needs
it to answer.

A changed device is recorded as an observation and never blocks a sitting. A
candidate whose laptop battery died and who resumed on a ward computer has done
nothing wrong, and locking them out of an examination would be a far worse
failure than the one it guards against.

### Consent

`may_capture_media` is the only function that can authorise camera or microphone
capture, and it requires a stored, un-withdrawn `ExamConsent` row *and* a policy
that asks for that stream. Consent cannot grant more than policy requests, and
withdrawal takes effect immediately. The exact wording the candidate agreed to
is copied onto the consent record, so a policy edited next term cannot rewrite
what someone consented to today.

### Retention

`purge_expired_events` deletes raw telemetry past its policy's retention period
(180 days by default). The summary report survives — it is the examination
record — but the event stream behind it does not, so a two-year-old sitting
cannot be re-litigated from keystroke-level data nobody agreed to keep. Run it
from the nightly maintenance job.

---

## 6. AI-assisted authoring

### The provider seam

`app/services/ai/provider.py` defines one interface with two implementations.
`AnthropicProvider` calls Claude through the official SDK; `MockProvider`
produces deterministic, structurally valid output with no network and no key.

**The offline provider is the default.** With `RTC_ENABLE_AI_GENERATION` unset,
every calling path works, the whole pipeline runs, and the API reports
`is_offline_placeholder: true` with a warning that the content is clinically
meaningless. A department can watch the entire workflow before deciding whether
to spend anything on it.

The mock deliberately emits one malformed item in seven, cycling through the
exact defects the quality gate exists to catch. A mock that only ever produced
perfect items would let a broken validator pass its tests forever.

### The nine stages

```
retrieve knowledge → generate → quality validation → blueprint validation
→ duplicate detection → difficulty balancing → assemble → await review → release
```

Each stage records its timing and its rejections onto the job, eagerly, so a
job that crashes still says where it got to.

**Quality validation** applies twelve checks drawn from the NBME item-writing
manual: exactly one key, five options, no absolute qualifiers, no "all of the
above", homogeneous options, a plausible stem length, no cueing word shared
between stem and key, an explanation, a rationale for *every* option, at least
one reference, complete curriculum mapping, and no patient-identifiable data.

Rejected drafts are kept with the specific checks they failed. "17 items
rejected" is useless; "9 lacked per-distractor rationales, 5 used absolute
terms, 3 were near-duplicates" is what fixes a prompt.

**Duplicate detection** uses word-bigram shingles and the overlap coefficient,
with a threshold of 0.45. That number was calibrated on measured data rather
than chosen: paraphrase pairs (same case, reworded) score 0.50–0.75 and
genuinely different items on the same topic score 0.00–0.14. Trigram Jaccard —
the more conventional choice — cannot separate them. `TestDuplicateDetection`
pins both sides of the boundary, so re-tuning it requires re-running the
evidence.

Items are compared against the live bank *and* against everything accepted
earlier in the same run, because two identical items inside one batch is the
common case rather than a rare one.

### Cost, and the twenty-minute target

The specification asks for a complete 50-item paper within twenty minutes of
topic approval. The job records `deadline_minutes`, its elapsed time, and
whether it met the target. **A run that overshoots is reported as having
overshot** rather than quietly succeeding.

Meeting it requires a background worker. On the container deployment this is
comfortable. **On Vercel serverless it is not achievable** — the same class of
constraint already documented in `docs/DEPLOYMENT_VERCEL.md` for migrations and
cohort scoring. The API exposes `run_now` for small batches and is explicit that
a fifty-item run belongs in the worker.

Two cost controls, both enforced *before* each call rather than after:

- `RTC_AI_JOB_COST_CEILING_USD` (default 5.00) halts a job that would exceed it,
  recording `halted: cost_ceiling` on the stage log.
- The retrieved knowledge context is placed behind a prompt-cache breakpoint, so
  the second and subsequent batches of a job read it at roughly a tenth of the
  input rate.

**On the ">100,000 questions per specialty" figure.** That is a generation-cost
claim, not an engineering one. At roughly 500 output tokens per fully-explained
item, 100,000 items is about 50 million output tokens per specialty — several
thousand US dollars at current Opus rates, before the review time. The pipeline,
the storage, the rotation and the non-repetition logic all scale to it; how many
items you actually generate is a budget decision, and the job reports its cost
so that decision can be made with a number in front of you.

### CME articles

`ARTICLE_SECTIONS` reproduces the prescribed 27-section structure in order. It
is a list rather than 27 columns because not every section applies to every
discipline — a radiology article has no operative technique — and an institution
should be able to drop those without a migration.

References are stored structurally and rendered in **both** Vancouver and APA
from one record. Asking an author to write each citation twice guarantees the
two will diverge.

The system prompt instructs the model not to invent references and to cite a
textbook when unsure a paper exists. That is a mitigation, not a guarantee:
`build_article` warns when no reference cites one of the authorities the
curriculum names, and the review screen tells the consultant explicitly that the
generator does not verify a cited paper exists.

---

## 7. Configuration

| Variable | Default | Effect |
|---|---|---|
| `RTC_ENABLE_AI_GENERATION` | `false` | Off means the deterministic offline provider. |
| `RTC_AI_API_KEY` | — | Anthropic API key. |
| `RTC_AI_MODEL` | `claude-opus-5` | Complete as written; never append a date suffix. |
| `RTC_AI_EFFORT` | `high` | `low` … `max`. |
| `RTC_AI_MAX_OUTPUT_TOKENS` | `32000` | Caps thinking *plus* visible output. |
| `RTC_AI_BATCH_SIZE` | `10` | Items per model call. |
| `RTC_AI_JOB_COST_CEILING_USD` | `5.00` | Hard ceiling per job, checked before each call. |
| `RTC_AI_INPUT_COST_PER_MTOK` | `5.0` | For the cost estimate; override if your contract differs. |
| `RTC_AI_OUTPUT_COST_PER_MTOK` | `25.0` | As above. |
| `RTC_AI_ENABLE_FALLBACKS` | `true` | Retry a safety-declined request on a fallback model. |
| `RTC_AI_GENERATION_DEADLINE_MINUTES` | `20` | The service level a job is measured against. |

Benign clinical and pharmacology content occasionally trips a safety classifier.
With fallbacks enabled the API re-runs the request on a suitable model inside
the same call; with them disabled a declined request simply stops. A refusal is
recorded on the job and does not fail the run.

---

## 8. Permissions

| Permission | Held by | Grants |
|---|---|---|
| `exam.attempt.take` | Trainees and above | Sit papers |
| `exam.question.review` | Department leadership and above | Operate the publication gate |
| `exam.question.generate` | Department leadership and above | Request generation |
| `exam.psychometrics.read` | Department leadership and above | Item and paper statistics |
| `exam.integrity.review` | Department leadership and above | Disposition integrity reports |
| `exam.integrity.configure` | Institution leadership | Set conduct policy |

`exam.question.review` sits at department level on purpose. A consultant is the
editorial gate for generated items; restricting the permission to institution
leadership would make the gate a weekly bottleneck, and a gate nobody can clear
is a gate that gets bypassed.

`exam.integrity.configure` sits higher, because how candidates are monitored,
whether cameras are used at all, and how long telemetry is kept are governance
questions rather than departmental ones.

---

*Created and managed by NEXORA Innovations.*
