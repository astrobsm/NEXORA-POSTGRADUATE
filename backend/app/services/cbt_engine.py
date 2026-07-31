"""Computer-based test delivery: assembly, sitting, marking and feedback.

The engine has four jobs and keeps them separate:

1. **Assembly** — turn a blueprint into a concrete, per-candidate list of items,
   honouring category proportions, difficulty mix, and a rotation rule that
   stops a trainee meeting the same item twice in a row.
2. **Sitting** — start, resume, answer, and submit, with the timer enforced
   server-side. A client that reports it has ten minutes left is not evidence.
3. **Marking** — deterministic scoring with partial credit where the item type
   calls for it.
4. **Feedback** — per-question review explaining why the key is right and why
   each distractor is wrong, with references and the article that teaches it.

Two rules run through all of it. Only items whose ``editorial_status`` is
``published`` are ever served, so unreviewed AI output cannot leak into an
examination. And the item order and option order a candidate saw are recorded on
the attempt, because a paper that cannot be reconstructed cannot be appealed.
"""

from __future__ import annotations

import hashlib
import random
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from app.db.base import owned_or_shared, utcnow
from app.models.cbt import ExamAttempt, ExamPaper, ExamResponse, Question, QuestionBank
from app.models.enums import (
    AttemptStatus,
    DifficultyBand,
    EditorialStatus,
    QuestionType,
)
from app.services.psychometrics import DIFFICULTY_BANDS, band_for_facility

# --------------------------------------------------------------------------
# Defaults the specification fixes. Every one is overridable per paper, per
# curriculum version, or per generation job — these are what you get if nobody
# expressed a preference.
# --------------------------------------------------------------------------
#: The examination blueprint, as proportions of the paper.
DEFAULT_BLUEPRINT: dict[str, float] = {
    "basic_sciences": 0.10,
    "clinical_medicine": 0.20,
    "operative_principles": 0.20,
    "emergency_management": 0.10,
    "investigations": 0.10,
    "complications": 0.10,
    "evidence_based_medicine": 0.10,
    "recent_guidelines": 0.05,
    "professionalism_ethics": 0.05,
}

#: The difficulty mix, as proportions of the paper. "Difficult" in the
#: specification maps onto the advanced and consultant bands together, split
#: two-to-one because consultant-standard items are scarcer in a young bank.
DEFAULT_DIFFICULTY_MIX: dict[str, float] = {
    DifficultyBand.EASY: 0.20,
    DifficultyBand.MODERATE: 0.40,
    DifficultyBand.ADVANCED: 0.20,
    DifficultyBand.CONSULTANT: 0.10,
    DifficultyBand.FELLOWSHIP: 0.10,
}

#: The primary mode: fifty single-best-answer items, five options, one key.
DEFAULT_QUESTION_COUNT = 50
DEFAULT_OPTION_COUNT = 5
DEFAULT_OPTION_KEYS = ["A", "B", "C", "D", "E"]

#: Weeks an item must sit out before the same trainee can meet it again.
DEFAULT_ROTATION_WEEKS = 12

#: Adaptive stepping. After each answer the target difficulty moves by this
#: much, up on a correct answer and down on a wrong one, bounded to [0, 1].
ADAPTIVE_STEP = 0.08


class AssemblyError(RuntimeError):
    """The bank cannot satisfy the request. Raised with what was missing."""

    def __init__(self, message: str, *, shortfall: dict[str, int] | None = None) -> None:
        super().__init__(message)
        self.shortfall = shortfall or {}


class SittingError(RuntimeError):
    """An operation is not valid for this attempt's state."""


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------
@dataclass(slots=True)
class AssemblyRequest:
    tenant_id: str
    bank_ids: list[str]
    count: int = DEFAULT_QUESTION_COUNT
    blueprint: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_BLUEPRINT))
    difficulty_mix: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_DIFFICULTY_MIX)
    )
    topics: list[str] = field(default_factory=list)
    #: Prefer items in these areas — the trainee's measured weak spots.
    weak_topics: list[str] = field(default_factory=list)
    #: Items this candidate has answered incorrectly before; worth revisiting.
    revisit_question_ids: list[str] = field(default_factory=list)
    #: Items to keep out entirely — recently served to this candidate.
    exclude_question_ids: list[str] = field(default_factory=list)
    training_levels: list[str] = field(default_factory=list)
    #: Deterministic assembly for tests and for reproducible personalised papers.
    seed: int | None = None
    #: Fail rather than under-deliver. Off by default: a 47-item paper a week
    #: early beats a 50-item paper that never ships.
    strict: bool = False


@dataclass(slots=True)
class AssemblyResult:
    question_ids: list[str]
    #: Delivered counts per blueprint category.
    category_counts: dict[str, int]
    #: Delivered counts per difficulty band.
    band_counts: dict[str, int]
    #: Where the bank could not meet the quota, and by how much.
    shortfall: dict[str, int]
    #: How many items were relaxed out of their requested band to fill the paper.
    substitutions: int
    pool_size: int

    @property
    def delivered(self) -> int:
        return len(self.question_ids)


def _quota(count: int, proportions: dict[str, float]) -> dict[str, int]:
    """Turn proportions into integer counts that sum to exactly ``count``.

    Largest-remainder apportionment. Naive rounding loses or gains items —
    nine categories rounded independently routinely yields 49 or 51 — and a
    50-item paper that silently contains 49 is the kind of defect that surfaces
    only when a candidate complains.
    """
    if count <= 0 or not proportions:
        return {}
    total = sum(proportions.values())
    if total <= 0:
        return {}

    exact = {k: count * (v / total) for k, v in proportions.items()}
    base = {k: int(v) for k, v in exact.items()}
    remaining = count - sum(base.values())
    order = sorted(exact, key=lambda k: (exact[k] - base[k], k), reverse=True)
    for key in order[:remaining]:
        base[key] += 1
    return base


def _servable_pool(
    db: Session, request: AssemblyRequest
) -> list[Question]:
    """Every item this request is allowed to draw on.

    ``owned_or_shared`` matters here: a platform-shared bank has a NULL tenant,
    and ``IN (tenant_id, NULL)`` would silently exclude it because SQL compares
    NULL with ``=``.
    """
    stmt = (
        select(Question)
        .join(QuestionBank, Question.bank_id == QuestionBank.id)
        .where(
            owned_or_shared(QuestionBank.tenant_id, request.tenant_id),
            Question.is_active.is_(True),
            Question.deleted_at.is_(None),
            # The publication gate. Unreviewed AI drafts are active, complete,
            # and must never reach a candidate.
            Question.editorial_status == EditorialStatus.PUBLISHED,
        )
    )
    if request.bank_ids:
        stmt = stmt.where(Question.bank_id.in_(request.bank_ids))
    if request.topics:
        stmt = stmt.where(Question.topic.in_(request.topics))
    if request.exclude_question_ids:
        stmt = stmt.where(Question.id.notin_(request.exclude_question_ids))
    return list(db.execute(stmt).scalars())


def _band_of(question: Question) -> str:
    if question.difficulty_band:
        return question.difficulty_band
    # difficulty is 0 (trivial) to 1 (very hard); facility is its complement.
    return band_for_facility(1.0 - (question.difficulty or 0.5))


def _priority(question: Question, request: AssemblyRequest) -> tuple[int, float]:
    """Sort key within a bucket. Lower sorts first.

    Ordering, most important first: items the candidate previously got wrong,
    then items on their weak topics, then items served least recently. The last
    is what keeps a large bank actually rotating rather than repeatedly serving
    whichever items happen to sort first.
    """
    tier = 3
    if question.id in request.revisit_question_ids:
        tier = 0
    elif question.topic and question.topic in request.weak_topics:
        tier = 1
    elif question.times_served == 0:
        tier = 2
    last = question.last_served_at.timestamp() if question.last_served_at else 0.0
    return (tier, last)


def assemble(db: Session, request: AssemblyRequest) -> AssemblyResult:
    """Draw a paper that matches the blueprint and difficulty mix as closely as
    the bank allows.

    The algorithm fills the (category, band) cells the two quotas imply, then
    relaxes in a fixed order: first the band constraint within the category
    (a category is a curriculum promise, a band is a calibration preference),
    then the category constraint. Every relaxation is counted and reported —
    a paper assembled by relaxing thirty of fifty cells is technically a paper
    and the department should be told.
    """
    rng = random.Random(request.seed)
    pool = _servable_pool(db, request)
    if not pool:
        raise AssemblyError("No published questions match this request.")

    category_quota = _quota(request.count, request.blueprint or DEFAULT_BLUEPRINT)
    if not category_quota:
        category_quota = {"unclassified": request.count}

    buckets: dict[tuple[str, str], list[Question]] = {}
    for question in pool:
        key = (question.blueprint_category or "unclassified", _band_of(question))
        buckets.setdefault(key, []).append(question)
    for items in buckets.values():
        rng.shuffle(items)
        items.sort(key=lambda q: _priority(q, request))

    chosen: list[Question] = []
    taken: set[str] = set()
    shortfall: dict[str, int] = {}
    substitutions = 0

    def take(category: str, band: str) -> Question | None:
        for question in buckets.get((category, band), []):
            if question.id not in taken:
                taken.add(question.id)
                return question
        return None

    for category, wanted in category_quota.items():
        if wanted <= 0:
            continue
        band_quota = _quota(wanted, request.difficulty_mix or DEFAULT_DIFFICULTY_MIX)
        filled = 0
        for band, band_wanted in band_quota.items():
            for _ in range(band_wanted):
                question = take(category, band)
                if question is not None:
                    chosen.append(question)
                    filled += 1
                    continue
                # Relaxation one: any band, same category. Bands nearest the
                # requested one are tried first so the mix degrades gracefully.
                for alternative in _nearest_bands(band):
                    question = take(category, alternative)
                    if question is not None:
                        chosen.append(question)
                        filled += 1
                        substitutions += 1
                        break
        if filled < wanted:
            shortfall[category] = wanted - filled

    # Relaxation two: fill any remaining places from anywhere in the pool,
    # respecting the difficulty mix as far as it still can be.
    if len(chosen) < request.count:
        leftovers = [q for q in pool if q.id not in taken]
        leftovers.sort(key=lambda q: _priority(q, request))
        for question in leftovers:
            if len(chosen) >= request.count:
                break
            taken.add(question.id)
            chosen.append(question)
            substitutions += 1

    if request.strict and len(chosen) < request.count:
        raise AssemblyError(
            f"Bank can supply only {len(chosen)} of {request.count} requested items.",
            shortfall=shortfall,
        )

    rng.shuffle(chosen)
    return AssemblyResult(
        question_ids=[q.id for q in chosen],
        category_counts=_count_by(chosen, lambda q: q.blueprint_category or "unclassified"),
        band_counts=_count_by(chosen, _band_of),
        shortfall=shortfall,
        substitutions=substitutions,
        pool_size=len(pool),
    )


def _nearest_bands(band: str) -> list[str]:
    """Difficulty bands ordered by distance from ``band``.

    Ordered so substitution moves to an adjacent band before a distant one:
    swapping a moderate item for an advanced one barely shifts the paper;
    swapping it for a fellowship-standard item changes what is being measured.
    """
    order = list(DIFFICULTY_BANDS)
    if band not in order:
        return order
    index = order.index(band)
    return sorted(
        (b for b in order if b != band), key=lambda b: abs(order.index(b) - index)
    )


def _count_by(questions: list[Question], key: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for question in questions:
        value = key(question)
        counts[value] = counts.get(value, 0) + 1
    return counts


def recent_question_ids(
    db: Session, user_id: str, *, weeks: int = DEFAULT_ROTATION_WEEKS
) -> list[str]:
    """Items this candidate has seen inside the rotation window.

    This is the non-repetition guarantee. It is a query rather than a stored
    list because attempts are the source of truth and a cached exclusion list
    would go stale exactly when it mattered.
    """
    cutoff = utcnow() - timedelta(weeks=weeks)
    rows = db.execute(
        select(ExamResponse.question_id)
        .join(ExamAttempt, ExamResponse.attempt_id == ExamAttempt.id)
        .where(ExamAttempt.user_id == user_id, ExamAttempt.started_at >= cutoff)
        .distinct()
    ).scalars()
    return list(rows)


def incorrect_question_ids(db: Session, user_id: str, *, limit: int = 200) -> list[str]:
    """Items this candidate has previously got wrong, most recent first."""
    rows = db.execute(
        select(ExamResponse.question_id)
        .join(ExamAttempt, ExamResponse.attempt_id == ExamAttempt.id)
        .where(
            ExamAttempt.user_id == user_id,
            ExamResponse.is_correct.is_(False),
        )
        .order_by(ExamResponse.created_at.desc())
        .limit(limit)
    ).scalars()
    # Preserve recency order while removing repeats.
    seen: set[str] = set()
    out: list[str] = []
    for qid in rows:
        if qid not in seen:
            seen.add(qid)
            out.append(qid)
    return out


def weak_topics(
    db: Session, user_id: str, *, threshold: float = 0.6, minimum_seen: int = 4
) -> list[str]:
    """Topics where this candidate scores below ``threshold``.

    ``minimum_seen`` exists because one wrong answer out of one is a 0% topic
    score and means nothing. Without the floor, the personalisation engine
    chases noise.
    """
    rows = db.execute(
        select(
            Question.topic,
            func.count(ExamResponse.id),
            # Cast because SQLite stores booleans as integers but PostgreSQL
            # will not sum a boolean column without being told to.
            func.sum(func.cast(ExamResponse.is_correct, Integer)),
        )
        .join(ExamResponse, ExamResponse.question_id == Question.id)
        .join(ExamAttempt, ExamResponse.attempt_id == ExamAttempt.id)
        .where(ExamAttempt.user_id == user_id, Question.topic.isnot(None))
        .group_by(Question.topic)
    ).all()

    weak: list[tuple[str, float]] = []
    for topic, seen, correct in rows:
        if not topic or seen < minimum_seen:
            continue
        accuracy = (correct or 0) / seen
        if accuracy < threshold:
            weak.append((topic, accuracy))
    weak.sort(key=lambda pair: pair[1])
    return [topic for topic, _ in weak]


# --------------------------------------------------------------------------
# Sitting
# --------------------------------------------------------------------------
def _option_order(question: Question, seed: str, shuffle: bool) -> list[str]:
    """A stable per-candidate option order.

    Derived from a hash of the attempt token and question id rather than stored,
    so it is identical on every request within a sitting — including after a
    reconnect — without a round trip. The candidate sees the same paper they
    left; the server can always reconstruct what they saw.
    """
    keys = [str(opt.get("key")) for opt in question.options if opt.get("key")]
    if not shuffle or len(keys) < 2:
        return keys
    digest = hashlib.sha256(f"{seed}:{question.id}".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    shuffled = list(keys)
    rng.shuffle(shuffled)
    return shuffled


def start_attempt(
    db: Session,
    *,
    paper: ExamPaper,
    user_id: str,
    enrolment_id: str | None = None,
    device_hash: str | None = None,
    network_hash: str | None = None,
    assembly: AssemblyResult | None = None,
    now: datetime | None = None,
) -> ExamAttempt:
    """Begin a sitting, or refuse with a reason.

    Refuses on: an unpublished paper, a closed window, an exhausted attempt
    allowance, and — the one that matters for integrity — an attempt already in
    progress. The in-progress case is not an error the candidate should be
    punished for; it is resumed by the caller via :func:`resume_attempt`.
    """
    now = now or utcnow()
    if not paper.is_published:
        raise SittingError("This paper has not been published.")
    if paper.opens_at and now < paper.opens_at:
        raise SittingError("This paper is not open yet.")
    if paper.closes_at and now > paper.closes_at:
        raise SittingError("This paper has closed.")
    if paper.target_user_id and paper.target_user_id != user_id:
        raise SittingError("This paper was issued to a different candidate.")

    previous = list(
        db.execute(
            select(ExamAttempt)
            .where(ExamAttempt.paper_id == paper.id, ExamAttempt.user_id == user_id)
            .order_by(ExamAttempt.attempt_number.desc())
        ).scalars()
    )
    live = [a for a in previous if a.status == AttemptStatus.IN_PROGRESS]
    if live:
        raise SittingError(
            "An attempt is already in progress. Resume it rather than starting again."
        )
    if paper.max_attempts is not None and len(previous) >= paper.max_attempts:
        raise SittingError(
            f"The maximum of {paper.max_attempts} attempt(s) has been used."
        )

    if assembly is None:
        if paper.question_ids:
            question_ids = list(paper.question_ids)
        else:
            assembly = assemble(
                db,
                AssemblyRequest(
                    tenant_id=paper.tenant_id,
                    bank_ids=[b.get("bank_id") for b in paper.blueprint if b.get("bank_id")],
                    count=paper.question_count,
                    blueprint=paper.blueprint_profile or DEFAULT_BLUEPRINT,
                    difficulty_mix=paper.difficulty_mix or DEFAULT_DIFFICULTY_MIX,
                    exclude_question_ids=recent_question_ids(db, user_id),
                ),
            )
            question_ids = assembly.question_ids
    else:
        question_ids = assembly.question_ids

    if not question_ids:
        raise SittingError("This paper has no questions to serve.")

    if paper.shuffle_questions:
        random.Random(f"{paper.id}:{user_id}").shuffle(question_ids)

    questions = _questions_in_order(db, question_ids)
    attempt = ExamAttempt(
        tenant_id=paper.tenant_id,
        paper_id=paper.id,
        user_id=user_id,
        enrolment_id=enrolment_id,
        attempt_number=len(previous) + 1,
        status=AttemptStatus.IN_PROGRESS,
        started_at=now,
        served_question_ids=question_ids,
        served_difficulties=[q.difficulty for q in questions],
        total_marks=sum(q.marks for q in questions),
        # A cryptographically random token, presented on every subsequent
        # request. This is what makes one-session-per-candidate enforceable.
        session_token=secrets.token_urlsafe(32),
        device_hash=device_hash,
        network_hash=network_hash,
    )
    db.add(attempt)
    db.flush()

    for sequence, question in enumerate(questions):
        db.add(
            ExamResponse(
                tenant_id=paper.tenant_id,
                attempt_id=attempt.id,
                question_id=question.id,
                sequence=sequence,
            )
        )
        question.last_served_at = now
        question.times_served += 1
    db.flush()
    return attempt


def _questions_in_order(db: Session, ids: list[str]) -> list[Question]:
    """Load questions preserving the requested order.

    ``IN`` returns rows in whatever order the planner likes, and the served
    order is part of the examination record.
    """
    rows = {
        q.id: q for q in db.execute(select(Question).where(Question.id.in_(ids))).scalars()
    }
    return [rows[qid] for qid in ids if qid in rows]


def resume_attempt(
    db: Session, attempt: ExamAttempt, *, session_token: str, now: datetime | None = None
) -> ExamAttempt:
    """Continue an interrupted sitting, refusing a mismatched session token."""
    now = now or utcnow()
    if attempt.status != AttemptStatus.IN_PROGRESS:
        raise SittingError("This attempt is no longer in progress.")
    if attempt.session_token and session_token != attempt.session_token:
        raise SittingError("This sitting is open in another session.")
    if remaining_seconds(db, attempt, now=now) <= 0:
        submit_attempt(db, attempt, now=now, auto=True)
        raise SittingError("The time allowed has expired; the attempt was submitted.")
    return attempt


def remaining_seconds(
    db: Session, attempt: ExamAttempt, *, now: datetime | None = None
) -> int:
    """Server-side clock. The client's own countdown is display only."""
    now = now or utcnow()
    paper = attempt.paper or db.get(ExamPaper, attempt.paper_id)
    limit = (paper.duration_minutes if paper else 0) * 60
    if limit <= 0:
        return 0
    elapsed = int((now - attempt.started_at).total_seconds())
    return max(0, limit - elapsed)


def record_answer(
    db: Session,
    attempt: ExamAttempt,
    *,
    question_id: str,
    selected_keys: list[str],
    seconds_spent: int = 0,
    flagged: bool = False,
    confidence: str | None = None,
    free_text: str | None = None,
    session_token: str | None = None,
    now: datetime | None = None,
) -> ExamResponse:
    """Store an answer. Marking happens at submission, never here.

    Deliberate: marking on the way in would mean the correct key has to be
    consulted, and anything the server does per-keystroke is a timing oracle.
    """
    now = now or utcnow()
    if attempt.status != AttemptStatus.IN_PROGRESS:
        raise SittingError("This attempt is no longer accepting answers.")
    if attempt.session_token and session_token and session_token != attempt.session_token:
        raise SittingError("This sitting is open in another session.")
    if remaining_seconds(db, attempt, now=now) <= 0:
        submit_attempt(db, attempt, now=now, auto=True)
        raise SittingError("The time allowed has expired; the attempt was submitted.")

    response = db.execute(
        select(ExamResponse).where(
            ExamResponse.attempt_id == attempt.id,
            ExamResponse.question_id == question_id,
        )
    ).scalar_one_or_none()
    if response is None:
        raise SittingError("That question was not served in this attempt.")

    response.selected_keys = list(selected_keys)
    response.free_text = free_text
    response.seconds_spent = max(response.seconds_spent, seconds_spent)
    response.flagged_for_review = flagged
    response.confidence = confidence
    db.flush()
    return response


# --------------------------------------------------------------------------
# Marking
# --------------------------------------------------------------------------
def mark_response(question: Question, selected: list[str]) -> tuple[bool, float]:
    """Score one response. Returns ``(is_correct, marks_awarded)``.

    Rules by item type:

    * **Single best answer** — all or nothing. There is one key.
    * **Multiple true/false** and **extended matching** — partial credit,
      proportional to correct selections, with wrong selections subtracting so
      that selecting everything scores zero rather than full marks. Negative
      totals are floored at zero: the platform does not do negative marking
      unless an institution asks, and none has.
    * **Short answer** — never auto-marked. Returned unmarked for a human.
    """
    keys = set(question.correct_keys or [])
    chosen = set(selected or [])
    marks = question.marks or 1.0

    if question.question_type == QuestionType.SHORT_ANSWER:
        return (False, 0.0)

    if question.question_type in (
        QuestionType.MULTIPLE_TRUE_FALSE,
        QuestionType.EXTENDED_MATCHING,
    ):
        if not keys:
            return (False, 0.0)
        right = len(chosen & keys)
        wrong = len(chosen - keys)
        fraction = max(0.0, (right - wrong) / len(keys))
        return (fraction >= 1.0, round(marks * fraction, 4))

    correct = bool(keys) and chosen == keys
    return (correct, marks if correct else 0.0)


def submit_attempt(
    db: Session,
    attempt: ExamAttempt,
    *,
    now: datetime | None = None,
    auto: bool = False,
) -> ExamAttempt:
    """Mark and close a sitting. Idempotent — resubmitting is not an error.

    Idempotence matters: an auto-submit on expiry and a candidate pressing
    Submit at the same moment is an ordinary race, not misconduct.
    """
    now = now or utcnow()
    if attempt.status in (AttemptStatus.SUBMITTED, AttemptStatus.MARKED):
        return attempt

    responses = list(
        db.execute(
            select(ExamResponse)
            .where(ExamResponse.attempt_id == attempt.id)
            .order_by(ExamResponse.sequence)
        ).scalars()
    )
    questions = {
        q.id: q
        for q in db.execute(
            select(Question).where(
                Question.id.in_([r.question_id for r in responses])
            )
        ).scalars()
    }

    scored = 0.0
    total = 0.0
    breakdown: dict[str, dict[str, Any]] = {}
    needs_human_marking = False

    for response in responses:
        question = questions.get(response.question_id)
        if question is None:
            continue
        total += question.marks or 1.0
        is_correct, marks = mark_response(question, response.selected_keys or [])
        if question.question_type == QuestionType.SHORT_ANSWER and response.free_text:
            needs_human_marking = True
            response.is_correct = None
        else:
            response.is_correct = is_correct
        response.marks_awarded = marks
        scored += marks
        if is_correct:
            question.times_correct += 1

        topic = question.topic or "unclassified"
        cell = breakdown.setdefault(
            topic, {"served": 0, "correct": 0, "marks": 0.0, "available": 0.0}
        )
        cell["served"] += 1
        cell["correct"] += 1 if is_correct else 0
        cell["marks"] += marks
        cell["available"] += question.marks or 1.0

    attempt.status = AttemptStatus.SUBMITTED if needs_human_marking else AttemptStatus.MARKED
    attempt.submitted_at = now
    attempt.seconds_used = int((now - attempt.started_at).total_seconds())
    attempt.total_marks = total
    attempt.scored_marks = round(scored, 4)
    attempt.percent_score = round(scored / total * 100, 2) if total > 0 else 0.0
    attempt.topic_breakdown = breakdown
    attempt.was_auto_submitted = auto

    paper = attempt.paper or db.get(ExamPaper, attempt.paper_id)
    if paper is not None and attempt.percent_score is not None:
        attempt.is_pass = attempt.percent_score >= paper.pass_mark_percent
    db.flush()

    _refresh_cohort_percentiles(db, attempt)
    return attempt


def _refresh_cohort_percentiles(db: Session, attempt: ExamAttempt) -> None:
    """Recompute percentiles for everyone who has sat this paper.

    Recomputed for the whole cohort rather than just the new attempt, because a
    percentile is a statement about a population and the population just
    changed. Cheap at cohort sizes a department produces; if a national sitting
    ever makes it expensive, it moves to the nightly job.
    """
    siblings = list(
        db.execute(
            select(ExamAttempt).where(
                ExamAttempt.paper_id == attempt.paper_id,
                ExamAttempt.percent_score.isnot(None),
                ExamAttempt.status.in_([AttemptStatus.SUBMITTED, AttemptStatus.MARKED]),
            )
        ).scalars()
    )
    if len(siblings) < 2:
        return
    scores = sorted(a.percent_score or 0.0 for a in siblings)
    for sibling in siblings:
        score = sibling.percent_score or 0.0
        below = sum(1 for s in scores if s < score)
        equal = sum(1 for s in scores if s == score)
        # Midpoint convention: ties share the rank between them rather than all
        # taking the optimistic end of the band.
        sibling.cohort_percentile = round((below + 0.5 * equal) / len(scores) * 100, 2)
    db.flush()


# --------------------------------------------------------------------------
# Adaptive delivery
# --------------------------------------------------------------------------
def adaptive_target(answered: int, correct: int) -> float:
    """The difficulty to aim the next item at, on the 0-1 scale.

    A running ability estimate that steps up on a correct answer and down on a
    wrong one, damped by how much evidence there is: the first answer moves the
    target by one step, the tenth barely moves it at all. Bounded to [0.05,
    0.95] so a run of correct answers cannot walk a candidate off the top of
    the bank and leave the assembler with nothing to serve.

    This is deliberately not item response theory. IRT needs calibrated item
    parameters from hundreds of candidates per item; a departmental bank in its
    first year has none, and fitting a three-parameter model to forty responses
    produces confident nonsense. The per-item statistics this platform already
    stores are exactly what a later IRT calibration would need, so this is a
    floor rather than a dead end.
    """
    if answered <= 0:
        return 0.5
    accuracy = correct / answered
    # sqrt damping: influence grows with evidence but with diminishing returns.
    weight = min(1.0, (answered**0.5) / 4)
    target = 0.5 + (accuracy - 0.5) * weight
    return round(min(0.95, max(0.05, target)), 4)


def next_adaptive_question(
    db: Session,
    attempt: ExamAttempt,
    *,
    answered: int,
    correct: int,
) -> Question | None:
    """The unanswered served item closest to the candidate's current target."""
    unanswered_ids = [
        row.question_id
        for row in db.execute(
            select(ExamResponse).where(
                ExamResponse.attempt_id == attempt.id,
                ExamResponse.is_correct.is_(None),
            )
        ).scalars()
        if not row.selected_keys
    ]
    if not unanswered_ids:
        return None

    target = adaptive_target(answered, correct)
    remaining = _questions_in_order(db, unanswered_ids)
    if not remaining:
        return None
    return min(remaining, key=lambda q: abs((q.difficulty or 0.5) - target))


@dataclass(slots=True)
class ServedQuestion:
    """One item as the candidate sees it. The key is not in here."""

    question_id: str
    sequence: int
    question_type: str
    stem: str
    lead_in: str | None
    #: Options in this candidate's order, without any correctness marker.
    options: list[dict[str, Any]]
    media_kind: str
    media_keys: list[str]
    marks: float
    suggested_seconds: int
    topic: str | None
    selected_keys: list[str]
    flagged_for_review: bool
    seconds_spent: int


def serve_questions(db: Session, attempt: ExamAttempt) -> list[ServedQuestion]:
    """The paper as the candidate should receive it.

    Correct keys, rationales and explanations are stripped here rather than in
    the API layer, so no future endpoint can accidentally serialise a whole
    ``Question`` and hand out the answers.
    """
    paper = attempt.paper or db.get(ExamPaper, attempt.paper_id)
    shuffle = bool(paper.shuffle_options) if paper else True
    seed = attempt.session_token or attempt.id

    responses = list(
        db.execute(
            select(ExamResponse)
            .where(ExamResponse.attempt_id == attempt.id)
            .order_by(ExamResponse.sequence)
        ).scalars()
    )
    questions = {
        q.id: q
        for q in db.execute(
            select(Question).where(Question.id.in_([r.question_id for r in responses]))
        ).scalars()
    }

    out: list[ServedQuestion] = []
    for response in responses:
        question = questions.get(response.question_id)
        if question is None:
            continue
        order = _option_order(question, seed, shuffle)
        by_key = {str(o.get("key")): o for o in question.options}
        out.append(
            ServedQuestion(
                question_id=question.id,
                sequence=response.sequence,
                question_type=question.question_type,
                stem=question.stem,
                lead_in=question.lead_in,
                options=[
                    {"key": key, "text": by_key[key].get("text")}
                    for key in order
                    if key in by_key
                ],
                media_kind=question.media_kind,
                media_keys=list(question.media_keys or []),
                marks=question.marks or 1.0,
                suggested_seconds=question.default_seconds,
                topic=question.topic,
                selected_keys=list(response.selected_keys or []),
                flagged_for_review=response.flagged_for_review,
                seconds_spent=response.seconds_spent,
            )
        )
    return out


# --------------------------------------------------------------------------
# Feedback
# --------------------------------------------------------------------------
@dataclass(slots=True)
class QuestionFeedback:
    question_id: str
    sequence: int
    stem: str
    lead_in: str | None
    selected_keys: list[str]
    correct_keys: list[str]
    is_correct: bool | None
    marks_awarded: float
    marks_available: float
    seconds_spent: int
    #: One entry per option: its text, whether it is the key, whether the
    #: candidate chose it, and why it is right or wrong.
    options: list[dict[str, Any]]
    explanation: str | None
    references: list[str]
    topic: str | None
    difficulty_band: str | None
    #: Cohort facility, so "I found that hard" can be checked against reality.
    cohort_facility: float | None
    #: The CME article that teaches this, when one is linked.
    cme_resource_id: str | None
    #: True when the item has not been reviewed since publication — shown so a
    #: candidate disputing an item knows its editorial state.
    authoring_source: str


def build_feedback(
    db: Session, attempt: ExamAttempt, *, include_unanswered: bool = True
) -> list[QuestionFeedback]:
    """Per-question review for a completed attempt.

    Refuses to produce anything for an attempt still in progress — feedback
    mid-sitting would hand the candidate the answers.
    """
    if attempt.status == AttemptStatus.IN_PROGRESS:
        raise SittingError("Feedback is not available until the attempt is submitted.")

    responses = list(
        db.execute(
            select(ExamResponse)
            .where(ExamResponse.attempt_id == attempt.id)
            .order_by(ExamResponse.sequence)
        ).scalars()
    )
    questions = {
        q.id: q
        for q in db.execute(
            select(Question).where(Question.id.in_([r.question_id for r in responses]))
        ).scalars()
    }

    out: list[QuestionFeedback] = []
    for response in responses:
        question = questions.get(response.question_id)
        if question is None:
            continue
        if not include_unanswered and not response.selected_keys:
            continue

        keys = set(question.correct_keys or [])
        chosen = set(response.selected_keys or [])
        options: list[dict[str, Any]] = []
        for option in question.options:
            key = str(option.get("key"))
            is_key = key in keys
            options.append(
                {
                    "key": key,
                    "text": option.get("text"),
                    "is_correct": is_key,
                    "was_selected": key in chosen,
                    # The per-distractor explanation the specification asks for.
                    # Falls back to a neutral statement rather than fabricating
                    # a reason the author did not give.
                    "rationale": option.get("rationale")
                    or (
                        "This is the single best answer."
                        if is_key
                        else "No author rationale was recorded for this option."
                    ),
                }
            )

        out.append(
            QuestionFeedback(
                question_id=question.id,
                sequence=response.sequence,
                stem=question.stem,
                lead_in=question.lead_in,
                selected_keys=list(response.selected_keys or []),
                correct_keys=list(question.correct_keys or []),
                is_correct=response.is_correct,
                marks_awarded=response.marks_awarded,
                marks_available=question.marks or 1.0,
                seconds_spent=response.seconds_spent,
                options=options,
                explanation=question.explanation,
                references=list(question.references or []),
                topic=question.topic,
                difficulty_band=question.difficulty_band or _band_of(question),
                cohort_facility=question.facility_index,
                cme_resource_id=question.cme_resource_id,
                authoring_source=question.authoring_source,
            )
        )
    return out
