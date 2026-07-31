"""The weekly CBT generation workflow.

Nine stages, run in order, each recorded on the job with its timing and its
rejections:

    retrieve knowledge -> generate -> quality validation -> blueprint
    validation -> duplicate detection -> difficulty balancing -> assemble
    -> await review -> release

Two things are worth stating plainly before the code.

**The twenty-minute target is a service level, not a guarantee.** The job
records ``deadline_minutes``, its actual elapsed time, and whether it met the
target. A run that overshoots is reported as having overshot rather than
quietly succeeding. On a container deployment the pipeline runs in a worker and
comfortably meets it; on serverless it cannot, and
``docs/DEPLOYMENT_VERCEL.md`` says so rather than the code pretending otherwise.

**Nothing here publishes.** ``release`` creates a paper from *already published*
questions. Generated items land as ``AI_DRAFT`` and become servable only when a
human with the review permission approves them. A job whose ``requires_review``
is false still cannot bypass that — it merely means the paper is created as
soon as enough of its items have been approved, rather than waiting for a
consultant to press a further button.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import owned_or_shared, utcnow
from app.models.cbt import ExamPaper, Question, QuestionBank
from app.models.cme import CmeResource
from app.models.enums import (
    AuthoringSource,
    EditorialStatus,
    ExamMode,
    GenerationStage,
    GenerationTrigger,
    QualityCheck,
    QuestionType,
)
from app.models.learning import GenerationJob, QuestionDraft, QuestionVersion
from app.services.ai.provider import AiProvider, AiUnavailable, Usage, get_provider
from app.services.ai.quality import (
    CheckResult,
    advisory_warnings,
    check_item,
    content_hash,
    find_duplicate,
    shingles,
)
from app.services.cbt_engine import (
    DEFAULT_BLUEPRINT,
    DEFAULT_DIFFICULTY_MIX,
    DEFAULT_QUESTION_COUNT,
    _quota,
)
from app.services.psychometrics import band_to_difficulty

#: Extra items to ask for beyond the target, because a proportion will be
#: rejected. A third over is roughly what the quality gate removes on a
#: well-tuned prompt; the pipeline regenerates if that turns out to be wrong.
OVERGENERATION_FACTOR = 1.35
#: Regeneration rounds allowed before the job settles for what it has. Three
#: is enough to recover from a bad batch; more usually means the prompt or the
#: topic list is the problem and burning budget will not fix it.
MAX_REGENERATION_ROUNDS = 3
#: Knowledge sources to put in front of the generator. Beyond this the context
#: stops improving items and starts costing money.
MAX_KNOWLEDGE_SOURCES = 12


class PipelineError(RuntimeError):
    """The job cannot proceed. The reason is recorded on the job."""


# --------------------------------------------------------------------------
# Prompting
# --------------------------------------------------------------------------
GENERATION_SYSTEM_PROMPT = """\
You write single-best-answer examination items for postgraduate medical and \
dental trainees sitting college fellowship examinations (NPMCN, WACS, WACP and \
equivalent bodies).

House style, which is not negotiable:

- Exactly five options, labelled A to E. Exactly one is correct.
- The stem is a clinical vignette. A competent candidate should be able to \
answer it from the stem and lead-in alone, before reading the options.
- Every option carries its own rationale: why the key is the single best \
answer, and for each distractor, why a reasonable candidate might choose it \
and why it is nonetheless wrong.
- No "all of the above", "none of the above", or "both A and B".
- No absolute qualifiers (always, never, all, none) in any option. Candidates \
are taught that options containing them are wrong, so they give the answer away.
- Options must be homogeneous: the same kind of thing, and of similar length. \
Do not elaborate the correct answer and leave the distractors terse.
- Do not repeat a distinctive word from the correct option in the stem.

Content requirements:

- Cite at least one specific reference per item: a named textbook with edition, \
a named guideline with its issuing body, or a journal article with its DOI.
- Never include patient-identifiable information. No names, no hospital or \
record numbers, no dates of birth, no contact details. Describe patients by \
age band and presenting features only.
- Do not invent references. If you are not confident a paper exists, cite a \
standard textbook instead.
- Map every item to a blueprint category, a topic, a difficulty band and a \
Bloom level, and report your own confidence in the item from 0 to 1.

Difficulty bands mean the proportion of trainees at the target level expected \
to answer correctly: easy 80-100%, moderate 60-80%, advanced 40-60%, \
consultant 20-40%, fellowship under 20%.\
"""


def question_schema(count: int) -> dict[str, Any]:
    """JSON schema constraining a batch of generated items."""
    return {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "stem": {"type": "string"},
                        "lead_in": {"type": "string"},
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "key": {
                                        "type": "string",
                                        "enum": ["A", "B", "C", "D", "E"],
                                    },
                                    "text": {"type": "string"},
                                    "is_correct": {"type": "boolean"},
                                    "rationale": {"type": "string"},
                                },
                                "required": ["key", "text", "is_correct", "rationale"],
                                "additionalProperties": False,
                            },
                        },
                        "explanation": {"type": "string"},
                        "references": {"type": "array", "items": {"type": "string"}},
                        "topic": {"type": "string"},
                        "subtopic": {"type": "string"},
                        "blueprint_category": {
                            "type": "string",
                            "enum": list(DEFAULT_BLUEPRINT),
                        },
                        "difficulty_band": {
                            "type": "string",
                            "enum": list(DEFAULT_DIFFICULTY_MIX),
                        },
                        "bloom_level": {
                            "type": "string",
                            "enum": [
                                "remember",
                                "understand",
                                "apply",
                                "analyse",
                                "evaluate",
                                "create",
                            ],
                        },
                        "competency_domain": {"type": "string"},
                        "learning_objectives": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "ai_confidence": {"type": "number"},
                    },
                    "required": [
                        "stem",
                        "lead_in",
                        "options",
                        "explanation",
                        "references",
                        "topic",
                        "blueprint_category",
                        "difficulty_band",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    }


def build_prompt(
    *,
    count: int,
    topics: list[str],
    objectives: list[str],
    level: str | None,
    specialty: str | None,
    category_quota: dict[str, int],
    band_quota: dict[str, int],
    avoid_stems: list[str],
) -> str:
    lines = [f"Write {count} single-best-answer items.", ""]
    lines.append(f"Topics: {', '.join(topics) if topics else 'general'}")
    if specialty:
        lines.append(f"Specialty: {specialty}")
    if level:
        lines.append(f"Target training level: {level}")
    if objectives:
        lines.append("")
        lines.append("Every item must serve one of these learning objectives:")
        lines.extend(f"  - {o}" for o in objectives)

    lines.extend(["", "Blueprint — distribute the items across these categories:"])
    for category, wanted in category_quota.items():
        if wanted:
            lines.append(f"  - {category.replace('_', ' ')}: {wanted} item(s)")

    lines.extend(["", "Difficulty mix:"])
    for band, wanted in band_quota.items():
        if wanted:
            lines.append(f"  - {band}: {wanted} item(s)")

    if avoid_stems:
        lines.extend(
            [
                "",
                "The bank already contains items opening as follows. Do not write "
                "anything that tests the same point, even reworded:",
            ]
        )
        lines.extend(f"  - {stem[:140]}" for stem in avoid_stems[:25])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Stage bookkeeping
# --------------------------------------------------------------------------
@dataclass(slots=True)
class StageRecord:
    stage: str
    started_at: float
    finished_at: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def close(self, **detail: Any) -> dict[str, Any]:
        self.finished_at = time.monotonic()
        self.detail.update(detail)
        return {
            "stage": self.stage,
            "seconds": round((self.finished_at - self.started_at), 3),
            **self.detail,
        }


class _Stages:
    """Records each stage's timing and outcome onto the job as it runs.

    Written eagerly rather than at the end so a job that crashes still says
    where it got to — which is the only useful thing a failed job can tell you.
    """

    def __init__(self, db: Session, job: GenerationJob) -> None:
        self.db = db
        self.job = job

    def run(self, stage: str) -> StageRecord:
        self.job.stage = stage
        self.db.flush()
        return StageRecord(stage=stage, started_at=time.monotonic())

    def done(self, record: StageRecord, **detail: Any) -> None:
        entry = record.close(**detail)
        self.job.stage_log = [*self.job.stage_log, entry]
        self.db.flush()


# --------------------------------------------------------------------------
# Stage 1 — knowledge retrieval
# --------------------------------------------------------------------------
#: Ranking weights for retrieved sources. Level of evidence dominates because a
#: guideline and a case report are not interchangeable inputs to an examination
#: item, whatever else they have in common.
EVIDENCE_RANK: dict[str, float] = {
    "1a": 1.00, "1b": 0.95,
    "guideline": 0.92,
    "2a": 0.85, "2b": 0.80,
    "3": 0.70, "4": 0.60, "5": 0.50,
    "textbook": 0.75,
}


def retrieve_knowledge(
    db: Session,
    *,
    tenant_id: str,
    topics: list[str],
    limit: int = MAX_KNOWLEDGE_SOURCES,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Rank the library's published material for the requested topics.

    Ranked by level of evidence, then recency, then how directly the source
    addresses the topic. Only published resources are eligible: generating
    items from an unreviewed AI-drafted article would compound one unverified
    artefact into fifty.
    """
    now = now or utcnow()
    candidates = list(
        db.execute(
            select(CmeResource).where(
                owned_or_shared(CmeResource.tenant_id, tenant_id),
                CmeResource.is_active.is_(True),
                CmeResource.deleted_at.is_(None),
                CmeResource.editorial_status == EditorialStatus.PUBLISHED,
            )
        ).scalars()
    )

    needles = [t.lower() for t in topics]
    scored: list[tuple[float, CmeResource, str]] = []
    for resource in candidates:
        resource_topics = [t.lower() for t in (resource.topics or [])]
        title = (resource.title or "").lower()
        if needles:
            direct = any(n in resource_topics for n in needles)
            loose = any(n in title for n in needles)
            if not direct and not loose:
                continue
            relevance = 1.0 if direct else 0.6
        else:
            relevance = 0.4

        evidence = EVIDENCE_RANK.get(resource.evidence_level or "textbook", 0.5)
        age_years = max(0, now.year - (resource.year or now.year))
        # Recency decays slowly: a 2015 landmark trial still outranks a 2024
        # narrative review, so this halves roughly every eight years.
        recency = 0.5 ** (age_years / 8)
        score = evidence * 0.5 + relevance * 0.3 + recency * 0.2
        matched = next(
            (n for n in needles if n in resource_topics or n in title), topics[0] if topics else ""
        )
        scored.append((score, resource, matched))

    scored.sort(key=lambda row: row[0], reverse=True)
    return [
        {
            "resource_id": resource.id,
            "title": resource.title,
            "source": resource.source,
            "year": resource.year,
            "doi": resource.doi,
            "evidence_level": resource.evidence_level,
            "topic_matched": matched,
            "rank_score": round(score, 4),
            "key_points": list(resource.key_points or [])[:8],
            "frequently_tested_areas": list(resource.frequently_tested_areas or [])[:8],
        }
        for score, resource, matched in scored[:limit]
    ]


def knowledge_context(sources: list[dict[str, Any]]) -> str:
    """Render retrieved sources as the cacheable half of the prompt.

    Stable across every batch in a job, which is why it is separated: it goes
    behind the cache breakpoint and is billed at a tenth of the input rate from
    the second batch onward.
    """
    if not sources:
        return ""
    lines = [
        "Reference material from this institution's published library. Prefer "
        "these over recollection, and cite them where they support an item.",
        "",
    ]
    for source in sources:
        header = f"[{source['evidence_level'] or 'unrated'}] {source['title']}"
        if source.get("year"):
            header += f" ({source['year']})"
        lines.append(header)
        if source.get("source"):
            lines.append(f"  Source: {source['source']}")
        if source.get("doi"):
            lines.append(f"  DOI: {source['doi']}")
        for point in source.get("key_points") or []:
            lines.append(f"  - {point}")
        for area in source.get("frequently_tested_areas") or []:
            lines.append(f"  Frequently tested: {area}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------
@dataclass(slots=True)
class PipelineResult:
    job: GenerationJob
    accepted: list[QuestionDraft]
    rejected: list[QuestionDraft]
    duplicates: list[QuestionDraft]
    questions: list[Question]
    paper: ExamPaper | None
    usage: Usage
    met_deadline: bool | None
    warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "job_id": self.job.id,
            "stage": self.job.stage,
            "generated": self.job.generated_count,
            "accepted": len(self.accepted),
            "rejected": len(self.rejected),
            "duplicates": len(self.duplicates),
            "questions_created": len(self.questions),
            "paper_id": self.paper.id if self.paper else None,
            "elapsed_seconds": self.job.elapsed_seconds,
            "met_deadline": self.met_deadline,
            "estimated_cost_usd": self.job.estimated_cost_usd,
            "warnings": self.warnings,
        }


def create_job(
    db: Session,
    *,
    tenant_id: str,
    bank_id: str | None,
    topics: list[str],
    learning_objectives: list[str] | None = None,
    count: int = DEFAULT_QUESTION_COUNT,
    org_unit_id: str | None = None,
    specialty_id: str | None = None,
    training_level: str | None = None,
    requested_by_id: str | None = None,
    target_user_id: str | None = None,
    trigger: str = GenerationTrigger.MANUAL,
    blueprint: dict[str, float] | None = None,
    difficulty_mix: dict[str, float] | None = None,
    requires_review: bool = True,
    now: datetime | None = None,
) -> GenerationJob:
    """Queue a generation job. Does not run it."""
    now = now or utcnow()
    job = GenerationJob(
        tenant_id=tenant_id,
        org_unit_id=org_unit_id,
        bank_id=bank_id,
        specialty_id=specialty_id,
        requested_by_id=requested_by_id,
        target_user_id=target_user_id,
        trigger=trigger,
        stage=GenerationStage.QUEUED,
        training_level=training_level,
        topics=list(topics),
        learning_objectives=list(learning_objectives or []),
        requested_count=count,
        blueprint=dict(blueprint or DEFAULT_BLUEPRINT),
        difficulty_mix=dict(difficulty_mix or DEFAULT_DIFFICULTY_MIX),
        requested_at=now,
        deadline_minutes=settings.ai_generation_deadline_minutes,
        requires_review=requires_review,
    )
    db.add(job)
    db.flush()
    return job


def run_job(
    db: Session,
    job: GenerationJob,
    *,
    provider: AiProvider | None = None,
    now: datetime | None = None,
) -> PipelineResult:
    """Execute every stage of one generation job.

    Commits nothing — the caller owns the transaction — but flushes at each
    stage so a concurrently-polling status endpoint sees real progress rather
    than a job stuck at "queued" until it finishes.
    """
    now = now or utcnow()
    provider = provider or get_provider()
    stages = _Stages(db, job)
    usage = Usage()
    warnings: list[str] = []

    job.started_at = now
    job.provider = provider.name
    job.model = provider.model
    db.flush()

    try:
        # ---- 1. Knowledge retrieval --------------------------------------
        record = stages.run(GenerationStage.RETRIEVING_KNOWLEDGE)
        sources = retrieve_knowledge(
            db, tenant_id=job.tenant_id, topics=job.topics, now=now
        )
        job.knowledge_sources = sources
        context = knowledge_context(sources)
        stages.done(record, sources=len(sources), context_chars=len(context))
        if not sources:
            warnings.append(
                "No published library material matched these topics, so items "
                "were generated from the model's own knowledge alone. Consider "
                "adding reference material before relying on this paper."
            )

        # ---- 2-6. Generate, validate, deduplicate ------------------------
        accepted, rejected, duplicates, generation_usage = _generate_until_enough(
            db, job, provider=provider, context=context, stages=stages
        )
        usage = usage + generation_usage
        job.generated_count = len(accepted) + len(rejected) + len(duplicates)
        job.accepted_count = len(accepted)
        job.rejected_count = len(rejected)
        job.duplicate_count = len(duplicates)

        # ---- 7. Difficulty balancing -------------------------------------
        record = stages.run(GenerationStage.DIFFICULTY_BALANCING)
        balanced, balance_detail = _balance(accepted, job)
        stages.done(record, **balance_detail)
        if balance_detail.get("dropped"):
            warnings.append(
                f"{balance_detail['dropped']} item(s) were set aside to hold the "
                "requested difficulty mix. They remain in the bank for future use."
            )

        # ---- 8. Assemble into the bank -----------------------------------
        record = stages.run(GenerationStage.ASSEMBLING)
        questions = _promote(db, job, balanced, now=now)
        stages.done(record, questions_created=len(questions))

        if len(questions) < job.requested_count:
            warnings.append(
                f"{len(questions)} usable item(s) were produced against "
                f"{job.requested_count} requested. The rejection detail on each "
                "draft says why."
            )

        # ---- 9. Await review ---------------------------------------------
        job.stage = GenerationStage.AWAITING_REVIEW
        job.finished_at = utcnow()
        job.input_tokens = usage.input_tokens + usage.cache_read_input_tokens
        job.output_tokens = usage.output_tokens
        job.estimated_cost_usd = usage.cost_usd()
        db.flush()

        met = job.met_deadline
        if met is False:
            warnings.append(
                f"The job took {job.elapsed_seconds:.0f}s against a "
                f"{job.deadline_minutes}-minute service level."
            )

        return PipelineResult(
            job=job,
            accepted=accepted,
            rejected=rejected,
            duplicates=duplicates,
            questions=questions,
            paper=None,
            usage=usage,
            met_deadline=met,
            warnings=warnings,
        )

    except AiUnavailable as exc:
        job.stage = GenerationStage.FAILED
        job.error = str(exc)
        job.finished_at = utcnow()
        db.flush()
        raise PipelineError(str(exc)) from exc


def _generate_until_enough(
    db: Session,
    job: GenerationJob,
    *,
    provider: AiProvider,
    context: str,
    stages: _Stages,
) -> tuple[list[QuestionDraft], list[QuestionDraft], list[QuestionDraft], Usage]:
    """Generate, check and deduplicate until the target is met or rounds run out."""
    accepted: list[QuestionDraft] = []
    rejected: list[QuestionDraft] = []
    duplicates: list[QuestionDraft] = []
    usage = Usage()
    sequence = 0

    known_hashes, known_shingles, existing_stems = _existing_fingerprints(db, job)

    for round_index in range(MAX_REGENERATION_ROUNDS):
        shortfall = job.requested_count - len(accepted)
        if shortfall <= 0:
            break

        wanted = max(1, round(shortfall * OVERGENERATION_FACTOR))
        category_quota = _quota(wanted, job.blueprint or DEFAULT_BLUEPRINT)
        band_quota = _quota(wanted, job.difficulty_mix or DEFAULT_DIFFICULTY_MIX)

        record = stages.run(GenerationStage.GENERATING)
        batch: list[dict[str, Any]] = []
        batch_usage = Usage()

        for offset in range(0, wanted, settings.ai_batch_size):
            batch_count = min(settings.ai_batch_size, wanted - offset)
            projected = usage.cost_usd() + batch_usage.cost_usd()
            if projected >= settings.ai_job_cost_ceiling_usd:
                # Checked before the call, not after. A ceiling enforced
                # afterwards is not a ceiling.
                stages.done(
                    record,
                    round=round_index,
                    halted="cost_ceiling",
                    spent_usd=round(projected, 4),
                    ceiling_usd=settings.ai_job_cost_ceiling_usd,
                )
                return accepted, rejected, duplicates, usage + batch_usage

            response = provider.structured(
                system=GENERATION_SYSTEM_PROMPT,
                prompt=build_prompt(
                    count=batch_count,
                    topics=job.topics,
                    objectives=job.learning_objectives,
                    level=job.training_level,
                    specialty=None,
                    category_quota=category_quota,
                    band_quota=band_quota,
                    # Showing the model what is already in the bank prevents
                    # far more duplicates than detecting them afterwards does,
                    # and costs a few hundred cached tokens rather than a
                    # discarded item.
                    avoid_stems=existing_stems,
                ),
                schema=question_schema(batch_count),
                cached_context=context,
            )
            batch_usage = batch_usage + response.usage
            if response.refused:
                # A refusal is a fact about this request, not a failure of the
                # job. Record it and carry on with the remaining batches.
                stages.done(
                    record,
                    round=round_index,
                    refused=True,
                    refusal_category=response.refusal_category,
                )
                record = stages.run(GenerationStage.GENERATING)
                continue
            batch.extend((response.data or {}).get("questions") or [])

        usage = usage + batch_usage
        stages.done(record, round=round_index, requested=wanted, returned=len(batch))

        # ---- quality validation ------------------------------------------
        record = stages.run(GenerationStage.QUALITY_VALIDATION)
        survivors: list[tuple[dict[str, Any], list[CheckResult]]] = []
        failure_counts: dict[str, int] = {}
        for item in batch:
            report = check_item(item)
            if report.passed:
                survivors.append((item, report.results))
            else:
                for failure in report.failures:
                    failure_counts[failure.check] = failure_counts.get(failure.check, 0) + 1
                rejected.append(
                    _draft(
                        db,
                        job,
                        item,
                        sequence=sequence,
                        accepted=False,
                        checks=report.as_list(),
                        reason=report.summary,
                    )
                )
                sequence += 1
        stages.done(
            record,
            round=round_index,
            passed=len(survivors),
            failed=len(batch) - len(survivors),
            failures_by_check=failure_counts,
        )

        # ---- blueprint validation ----------------------------------------
        record = stages.run(GenerationStage.BLUEPRINT_VALIDATION)
        off_blueprint = [
            item
            for item, _ in survivors
            if item.get("blueprint_category") not in (job.blueprint or DEFAULT_BLUEPRINT)
        ]
        stages.done(
            record,
            round=round_index,
            in_blueprint=len(survivors) - len(off_blueprint),
            off_blueprint=len(off_blueprint),
        )

        # ---- duplicate detection -----------------------------------------
        record = stages.run(GenerationStage.DUPLICATE_DETECTION)
        found = 0
        for item, checks in survivors:
            item_hash = content_hash(item["stem"], item.get("options"))
            item_shingles = shingles(item["stem"])
            match = find_duplicate(
                item_hash=item_hash,
                item_shingles=item_shingles,
                known_hashes=known_hashes,
                known_shingles=known_shingles,
            )
            if match is not None:
                found += 1
                duplicates.append(
                    _draft(
                        db,
                        job,
                        item,
                        sequence=sequence,
                        accepted=False,
                        checks=[
                            *checks_as_list(checks),
                            {
                                "check": QualityCheck.NOT_DUPLICATE,
                                "passed": False,
                                "detail": (
                                    f"{match.kind} match, similarity "
                                    f"{match.score:.2f}, against "
                                    f"{match.existing_id}."
                                ),
                            },
                        ],
                        reason=(
                            f"{match.kind.title()} duplicate of an existing item "
                            f"(similarity {match.score:.2f})."
                        ),
                        content_hash=item_hash,
                        item_shingles=item_shingles,
                        duplicate_of_id=(
                            match.existing_id
                            if match.existing_id and not match.existing_id.startswith("draft:")
                            else None
                        ),
                    )
                )
                sequence += 1
                continue

            draft = _draft(
                db,
                job,
                item,
                sequence=sequence,
                accepted=True,
                checks=[
                    *checks_as_list(checks),
                    {"check": QualityCheck.NOT_DUPLICATE, "passed": True, "detail": ""},
                ],
                reason=None,
                content_hash=item_hash,
                item_shingles=item_shingles,
            )
            sequence += 1
            accepted.append(draft)
            # Register immediately so two items inside one batch cannot
            # duplicate each other — which they otherwise routinely do.
            known_hashes[item_hash] = f"draft:{draft.id}"
            known_shingles[f"draft:{draft.id}"] = item_shingles

        stages.done(record, round=round_index, duplicates=found)
        job.regeneration_rounds = round_index + 1
        db.flush()

    return accepted, rejected, duplicates, usage


def checks_as_list(results: list[CheckResult]) -> list[dict[str, Any]]:
    return [r.as_dict() for r in results]


def _existing_fingerprints(
    db: Session, job: GenerationJob
) -> tuple[dict[str, str], dict[str, list[str]], list[str]]:
    """Fingerprints of items already in the bank, for duplicate detection.

    Narrowed to the job's own bank and topics. Comparing a new item against a
    hundred thousand shingle sets would dominate the twenty-minute budget, and
    an item duplicating something in a different specialty's bank is not a
    problem worth that cost.

    Also returns a sample of existing stems, which go into the prompt. Telling
    the model what already exists prevents more duplicates than detecting them
    afterwards does, and a prevented duplicate costs nothing where a detected
    one costs a whole generated item.
    """
    stmt = select(Question).where(
        Question.tenant_id == job.tenant_id,
        Question.deleted_at.is_(None),
    )
    if job.bank_id:
        stmt = stmt.where(Question.bank_id == job.bank_id)
    if job.topics:
        stmt = stmt.where(Question.topic.in_(job.topics))

    hashes: dict[str, str] = {}
    shingle_sets: dict[str, list[str]] = {}
    stems: list[str] = []
    for question in db.execute(stmt.limit(5000)).scalars():
        fingerprint = question.content_hash or content_hash(
            question.stem, question.options
        )
        hashes[fingerprint] = question.id
        shingle_sets[question.id] = list(question.shingles or []) or shingles(
            question.stem
        )
        if len(stems) < 25:
            stems.append(question.stem)
    return hashes, shingle_sets, stems


def _draft(
    db: Session,
    job: GenerationJob,
    item: dict[str, Any],
    *,
    sequence: int,
    accepted: bool,
    checks: list[dict[str, Any]],
    reason: str | None,
    content_hash: str | None = None,
    item_shingles: list[str] | None = None,
    duplicate_of_id: str | None = None,
) -> QuestionDraft:
    from app.services.ai.quality import content_hash as compute_hash
    from app.services.ai.quality import shingles as compute_shingles

    stem = str(item.get("stem") or "")
    options = item.get("options") or []
    draft = QuestionDraft(
        tenant_id=job.tenant_id,
        job_id=job.id,
        sequence=sequence,
        question_type=QuestionType.SINGLE_BEST_ANSWER,
        stem=stem,
        lead_in=item.get("lead_in"),
        options=options,
        correct_keys=[
            str(o.get("key")) for o in options if o.get("is_correct")
        ],
        explanation=item.get("explanation"),
        references=list(item.get("references") or []),
        topic=item.get("topic"),
        subtopic=item.get("subtopic"),
        blueprint_category=item.get("blueprint_category"),
        difficulty=band_to_difficulty(str(item.get("difficulty_band") or "moderate")),
        difficulty_band=item.get("difficulty_band"),
        bloom_level=item.get("bloom_level"),
        competency_domain=item.get("competency_domain"),
        learning_objectives=list(item.get("learning_objectives") or []),
        ai_confidence=item.get("ai_confidence"),
        content_hash=content_hash or compute_hash(stem, options),
        shingles=item_shingles or compute_shingles(stem),
        is_accepted=accepted,
        check_results=checks,
        rejection_reason=reason,
        duplicate_of_id=duplicate_of_id,
    )
    db.add(draft)
    db.flush()
    return draft


def _balance(
    drafts: list[QuestionDraft], job: GenerationJob
) -> tuple[list[QuestionDraft], dict[str, Any]]:
    """Trim the accepted set to the requested difficulty mix.

    Surplus in an over-supplied band is set aside rather than discarded — those
    drafts still become bank items, they just do not go into this paper. A bank
    grows from every job whether or not the job's own paper needed the items.
    """
    target = _quota(job.requested_count, job.difficulty_mix or DEFAULT_DIFFICULTY_MIX)
    by_band: dict[str, list[QuestionDraft]] = {}
    for draft in drafts:
        by_band.setdefault(draft.difficulty_band or "moderate", []).append(draft)

    kept: list[QuestionDraft] = []
    delivered: dict[str, int] = {}
    for band, wanted in target.items():
        available = by_band.get(band, [])
        # Highest-confidence items first when there is a surplus to choose from.
        available.sort(key=lambda d: d.ai_confidence or 0.0, reverse=True)
        take = available[:wanted]
        kept.extend(take)
        delivered[band] = len(take)

    # Any shortfall is filled from whatever bands over-supplied, because a
    # 47-item paper with a slightly wrong mix beats a 41-item paper with a
    # perfect one.
    if len(kept) < job.requested_count:
        chosen = {d.id for d in kept}
        spare = [d for d in drafts if d.id not in chosen]
        spare.sort(key=lambda d: d.ai_confidence or 0.0, reverse=True)
        kept.extend(spare[: job.requested_count - len(kept)])

    return kept, {
        "target": target,
        "delivered": delivered,
        "kept": len(kept),
        "dropped": max(0, len(drafts) - len(kept)),
    }


def _promote(
    db: Session,
    job: GenerationJob,
    drafts: list[QuestionDraft],
    *,
    now: datetime,
) -> list[Question]:
    """Turn accepted drafts into bank items at ``AI_DRAFT``.

    The status is the whole point of this function: these items exist, they are
    complete, they are mapped to the curriculum, and no trainee can be served
    one until a human moves it to published.
    """
    bank_id = job.bank_id
    if bank_id is None:
        bank = db.execute(
            select(QuestionBank)
            .where(
                QuestionBank.tenant_id == job.tenant_id,
                QuestionBank.is_active.is_(True),
                QuestionBank.deleted_at.is_(None),
            )
            .limit(1)
        ).scalar_one_or_none()
        if bank is None:
            raise PipelineError(
                "No question bank was supplied and the institution has none. "
                "Create a bank before generating items."
            )
        bank_id = bank.id
        job.bank_id = bank_id

    created: list[Question] = []
    for draft in drafts:
        question = Question(
            tenant_id=job.tenant_id,
            bank_id=bank_id,
            question_type=draft.question_type,
            stem=draft.stem,
            lead_in=draft.lead_in,
            options=draft.options,
            correct_keys=draft.correct_keys,
            explanation=draft.explanation,
            references=draft.references,
            difficulty=draft.difficulty,
            difficulty_band=draft.difficulty_band,
            topic=draft.topic,
            subtopic=draft.subtopic,
            blueprint_category=draft.blueprint_category,
            bloom_level=draft.bloom_level,
            competency_domain=draft.competency_domain,
            learning_objectives=draft.learning_objectives,
            author_id=job.requested_by_id,
            authoring_source=AuthoringSource.AI_GENERATED,
            editorial_status=EditorialStatus.AI_DRAFT,
            generation_job_id=job.id,
            ai_confidence=draft.ai_confidence,
            content_hash=draft.content_hash,
            shingles=draft.shingles,
            # Training level is a property of the bank and of the paper, not of
            # an individual item: the same item on biliary anatomy is fair for a
            # registrar and for a fellow, and what differs is the paper it lands
            # in. The level therefore travels on the released paper.
            tags=[job.training_level] if job.training_level else [],
        )
        db.add(question)
        db.flush()

        # Advisory style notes travel with the version so the reviewer sees
        # them beside the item rather than having to re-derive them.
        notes = advisory_warnings(
            {
                "stem": question.stem,
                "lead_in": question.lead_in,
                "ai_confidence": question.ai_confidence,
            }
        )
        db.add(
            QuestionVersion(
                tenant_id=job.tenant_id,
                question_id=question.id,
                version=1,
                editorial_status=EditorialStatus.AI_DRAFT,
                snapshot={
                    "stem": question.stem,
                    "lead_in": question.lead_in,
                    "options": question.options,
                    "correct_keys": question.correct_keys,
                    "explanation": question.explanation,
                    "references": question.references,
                },
                change_summary=(
                    f"Generated by {job.provider}/{job.model} in job {job.id}. "
                    f"Advisory notes: {'; '.join(notes) or 'none'}."
                ),
                changed_by_id=job.requested_by_id,
            )
        )
        draft.promoted_question_id = question.id
        created.append(question)

    db.flush()
    return created


# --------------------------------------------------------------------------
# Release
# --------------------------------------------------------------------------
def release_paper(
    db: Session,
    job: GenerationJob,
    *,
    released_by_id: str,
    name: str | None = None,
    duration_minutes: int = 90,
    pass_mark_percent: float = 50.0,
    integrity_policy_id: str | None = None,
    cycle_year: int | None = None,
    cycle_week: int | None = None,
    now: datetime | None = None,
) -> ExamPaper:
    """Create a paper from this job's items that have been approved.

    Refuses when too few items have cleared review. That refusal is the
    editorial gate doing its job: a weekly paper that quietly ships with twelve
    reviewed items and thirty-eight unreviewed ones would defeat the entire
    control, so the caller is told to finish reviewing instead.
    """
    now = now or utcnow()
    questions = list(
        db.execute(
            select(Question).where(
                Question.generation_job_id == job.id,
                Question.editorial_status == EditorialStatus.PUBLISHED,
                Question.is_active.is_(True),
                Question.deleted_at.is_(None),
            )
        ).scalars()
    )
    if len(questions) < job.requested_count:
        raise PipelineError(
            f"Only {len(questions)} of {job.requested_count} generated items have "
            "been approved for publication. Complete the review queue, or release "
            "a shorter paper by lowering the requested count."
        )

    paper = ExamPaper(
        tenant_id=job.tenant_id,
        org_unit_id=job.org_unit_id,
        name=name or _default_paper_name(job, now),
        description=(
            f"Generated on {now:%d %b %Y} from job {job.id}. "
            f"Items were reviewed and approved before release."
        ),
        mode=ExamMode.FORMATIVE,
        question_ids=[q.id for q in questions[: job.requested_count]],
        question_count=job.requested_count,
        duration_minutes=duration_minutes,
        pass_mark_percent=pass_mark_percent,
        blueprint_profile=dict(job.blueprint or {}),
        difficulty_mix=dict(job.difficulty_mix or {}),
        integrity_policy_id=integrity_policy_id,
        target_user_id=job.target_user_id,
        generated_by_job_id=job.id,
        cycle_year=cycle_year,
        cycle_week=cycle_week,
        applies_to_levels=[job.training_level] if job.training_level else [],
        is_published=True,
    )
    db.add(paper)
    db.flush()

    job.released_paper_id = paper.id
    job.released_at = now
    job.released_by_id = released_by_id
    job.stage = GenerationStage.RELEASED
    db.flush()
    return paper


def _default_paper_name(job: GenerationJob, now: datetime) -> str:
    topics = ", ".join(job.topics[:3]) or "General"
    if job.target_user_id:
        return f"Personalised paper: {topics} ({now:%d %b %Y})"
    year, week, _ = now.isocalendar()
    return f"Weekly CBT {year} week {week}: {topics}"
