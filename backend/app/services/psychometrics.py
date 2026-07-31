"""Classical test theory: item and paper statistics.

Everything here is a pure function over a response matrix. The database layer is
a thin shell at the bottom of the module, so the statistics can be tested against
worked textbook examples without a session — which is the only way to be
confident they are right, because a subtly wrong reliability coefficient looks
exactly like a correct one.

Conventions used throughout:

* A **response matrix** is ``list[list[float]]`` — one row per candidate, one
  column per item, holding marks awarded. For dichotomous items that is 0 or 1.
* ``None`` is returned rather than a placeholder number whenever a statistic is
  undefined (fewer than two candidates, zero total variance, an item everyone
  answered identically). A reliability of 0.0 and an undefined reliability mean
  very different things to an examinations officer.

References for the formulae: Kuder & Richardson (1937) for KR-20; Cronbach
(1951) for alpha; Ebel & Frisbie, *Essentials of Educational Measurement*, for
the discrimination-index bands and the 27% upper/lower convention.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.cbt import ExamAttempt, ExamPaper, ExamResponse, Question
from app.models.enums import AttemptStatus, DifficultyBand
from app.models.learning import ItemAnalysis, PaperAnalysis

# --------------------------------------------------------------------------
# Interpretation thresholds
#
# These are the conventional bands. They are module constants rather than
# configuration because they are properties of the statistics, not of an
# institution's policy — an item with a point-biserial of 0.05 is failing to
# discriminate in Owerri exactly as it is in Ibadan.
# --------------------------------------------------------------------------
#: Below this, an item is not distinguishing strong candidates from weak ones.
POOR_DISCRIMINATION = 0.20
#: A negative point-biserial means the *weaker* candidates did better: almost
#: always a miskeyed item, and the single most valuable thing this module finds.
NEGATIVE_DISCRIMINATION = 0.0
#: Facility outside this range carries little information either way.
TOO_EASY = 0.90
TOO_HARD = 0.20
#: A distractor chosen by fewer than this share of candidates is doing no work.
NON_FUNCTIONING_DISTRACTOR = 0.05
#: Ebel's convention for the upper and lower contrast groups.
CONTRAST_GROUP_FRACTION = 0.27
#: Reliability below this makes a summative pass/fail decision hard to defend.
MINIMUM_DEFENSIBLE_RELIABILITY = 0.70

#: Facility ranges for the five named difficulty bands. Facility is the
#: proportion answering *correctly*, so the hardest band has the lowest range.
DIFFICULTY_BANDS: dict[str, tuple[float, float]] = {
    DifficultyBand.EASY: (0.80, 1.01),
    DifficultyBand.MODERATE: (0.60, 0.80),
    DifficultyBand.ADVANCED: (0.40, 0.60),
    DifficultyBand.CONSULTANT: (0.20, 0.40),
    DifficultyBand.FELLOWSHIP: (0.00, 0.20),
}


def band_for_facility(facility: float) -> str:
    """Name the difficulty band a facility index falls into."""
    for band, (low, high) in DIFFICULTY_BANDS.items():
        if low <= facility < high:
            return band
    return DifficultyBand.MODERATE


def band_to_difficulty(band: str) -> float:
    """The midpoint difficulty (1 - facility) for a named band.

    Used when seeding a brand-new item that has no live statistics yet: a
    consultant says "fellowship standard" and the assembler needs a number.
    """
    low, high = DIFFICULTY_BANDS.get(band, (0.60, 0.80))
    midpoint_facility = (low + min(high, 1.0)) / 2
    return round(1.0 - midpoint_facility, 3)


# --------------------------------------------------------------------------
# Pure statistics
# --------------------------------------------------------------------------
def _variance(values: list[float]) -> float:
    """Population variance.

    Population rather than sample is deliberate: KR-20 and alpha are defined
    over the observed group, which *is* the population of interest. Using the
    sample variance inflates both by a factor of n/(n-1) applied twice, which
    happens to nearly cancel — so the error is small, plausible, and very hard
    to spot. Hence this note.
    """
    n = len(values)
    if n == 0:
        return 0.0
    mean = sum(values) / n
    return sum((v - mean) ** 2 for v in values) / n


def facility_index(item_scores: list[float], max_mark: float = 1.0) -> float | None:
    """Mean proportion of the available mark achieved on one item."""
    if not item_scores or max_mark <= 0:
        return None
    return sum(item_scores) / (len(item_scores) * max_mark)


def point_biserial(item_scores: list[float], total_scores: list[float]) -> float | None:
    """Correlation between performance on one item and on the paper as a whole.

    Computed against the *rest* score — the total with this item's contribution
    removed. Without that correction every item correlates with itself through
    the total, which inflates discrimination on short papers badly enough to
    make a 20-item quiz look better than a 200-item exam.
    """
    n = len(item_scores)
    if n < 2 or n != len(total_scores):
        return None

    rest = [total - item for total, item in zip(total_scores, item_scores, strict=True)]
    item_sd = math.sqrt(_variance(item_scores))
    rest_sd = math.sqrt(_variance(rest))
    if item_sd == 0 or rest_sd == 0:
        # Everyone scored the same on the item, or on everything else. The
        # correlation is undefined, not zero.
        return None

    item_mean = sum(item_scores) / n
    rest_mean = sum(rest) / n
    covariance = (
        sum((i - item_mean) * (r - rest_mean) for i, r in zip(item_scores, rest, strict=True))
        / n
    )
    return covariance / (item_sd * rest_sd)


def discrimination_index(
    item_scores: list[float],
    total_scores: list[float],
    *,
    fraction: float = CONTRAST_GROUP_FRACTION,
    max_mark: float = 1.0,
) -> float | None:
    """Upper-group facility minus lower-group facility (Ebel's D).

    Cruder than the point-biserial but far easier to explain to a committee,
    which is why examination boards still ask for it.
    """
    n = len(item_scores)
    if n < 2 or n != len(total_scores) or max_mark <= 0:
        return None

    group_size = max(1, round(n * fraction))
    if group_size * 2 > n:
        # Too few candidates to form two disjoint groups.
        group_size = n // 2
        if group_size == 0:
            return None

    order = sorted(range(n), key=lambda i: total_scores[i], reverse=True)
    upper = [item_scores[i] for i in order[:group_size]]
    lower = [item_scores[i] for i in order[-group_size:]]
    return (sum(upper) - sum(lower)) / (group_size * max_mark)


def kr20(matrix: list[list[float]]) -> float | None:
    """Kuder-Richardson 20 for dichotomously scored items.

    Undefined — and returned as ``None`` — when total score variance is zero,
    which happens when every candidate scored identically. A paper on which
    nobody varied has no reliability to measure, and reporting 0.0 would read
    as "unreliable" when the truth is "unmeasurable".
    """
    if len(matrix) < 2:
        return None
    k = len(matrix[0]) if matrix else 0
    if k < 2:
        return None

    totals = [sum(row) for row in matrix]
    total_var = _variance(totals)
    if total_var == 0:
        return None

    item_var_sum = 0.0
    for j in range(k):
        column = [row[j] for row in matrix]
        p = sum(column) / len(column)
        item_var_sum += p * (1 - p)

    return (k / (k - 1)) * (1 - item_var_sum / total_var)


def cronbach_alpha(matrix: list[list[float]]) -> float | None:
    """Cronbach's alpha — the general case, valid for partial credit.

    Identical to KR-20 when every item is scored 0 or 1. Both are computed and
    stored because a paper that mixes SBAs with partially-credited EMQs needs
    alpha, and a board that asked for KR-20 will not accept "they are the same
    thing here" without seeing both.
    """
    if len(matrix) < 2:
        return None
    k = len(matrix[0]) if matrix else 0
    if k < 2:
        return None

    totals = [sum(row) for row in matrix]
    total_var = _variance(totals)
    if total_var == 0:
        return None

    item_var_sum = sum(_variance([row[j] for row in matrix]) for j in range(k))
    return (k / (k - 1)) * (1 - item_var_sum / total_var)


def standard_error_of_measurement(
    matrix: list[list[float]], reliability: float | None
) -> float | None:
    """SEM in raw mark units: sd * sqrt(1 - reliability).

    This is what turns a borderline result into an honest one. A candidate two
    marks below the pass mark on a paper with an SEM of four marks has not
    demonstrably failed.
    """
    if reliability is None or len(matrix) < 2:
        return None
    totals = [sum(row) for row in matrix]
    sd = math.sqrt(_variance(totals))
    # Reliability estimates can come out slightly negative on pathological
    # data; the square root would then be imaginary.
    return sd * math.sqrt(max(0.0, 1.0 - reliability))


def distractor_analysis(
    selections: list[list[str]],
    total_scores: list[float],
    option_keys: list[str],
    correct_keys: list[str],
    *,
    fraction: float = CONTRAST_GROUP_FRACTION,
) -> dict[str, dict[str, Any]]:
    """Who chose what, split by whether they did well overall.

    The two findings worth acting on:

    * a distractor nobody chose is wasted — the item is effectively four
      options masquerading as five;
    * a distractor chosen *more* by the upper group than the lower group is
      attracting the candidates who know most, which usually means it is
      defensible and the key is wrong.
    """
    n = len(selections)
    stats: dict[str, dict[str, Any]] = {}
    if n == 0:
        return stats

    group_size = max(1, round(n * fraction))
    if group_size * 2 > n:
        group_size = max(1, n // 2)
    order = sorted(range(n), key=lambda i: total_scores[i], reverse=True)
    upper_ids = set(order[:group_size])
    lower_ids = set(order[-group_size:])

    for key in option_keys:
        chosen = [i for i, sel in enumerate(selections) if key in sel]
        upper = sum(1 for i in chosen if i in upper_ids)
        lower = sum(1 for i in chosen if i in lower_ids)
        is_key = key in correct_keys
        share = len(chosen) / n
        flags: list[str] = []
        if not is_key and share < NON_FUNCTIONING_DISTRACTOR:
            flags.append("non_functioning")
        if not is_key and upper > lower:
            flags.append("attracts_strong_candidates")
        if is_key and lower > upper:
            flags.append("key_favours_weak_candidates")
        stats[key] = {
            "count": len(chosen),
            "share": round(share, 4),
            "is_key": is_key,
            "upper": upper,
            "lower": lower,
            "flags": flags,
        }
    # Candidates who answered nothing still count toward the denominator, so
    # the shares will not sum to 1 on a paper with omissions. That is correct
    # and intended: an item nobody attempted is a finding in itself.
    return stats


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
@dataclass(slots=True)
class ItemStatistics:
    question_id: str
    candidates: int
    facility: float | None
    discrimination: float | None
    discrimination_index: float | None
    mean_seconds: float | None
    distractor_stats: dict[str, dict[str, Any]]
    flags: list[str] = field(default_factory=list)

    @property
    def difficulty(self) -> float | None:
        """Difficulty on the platform's 0 (trivial) to 1 (very hard) scale."""
        return None if self.facility is None else round(1.0 - self.facility, 4)

    @property
    def band(self) -> str | None:
        return None if self.facility is None else band_for_facility(self.facility)


@dataclass(slots=True)
class PaperStatistics:
    paper_id: str
    candidates: int
    items: int
    mean_percent: float | None
    sd_percent: float | None
    median_percent: float | None
    pass_rate: float | None
    kr20: float | None
    cronbach_alpha: float | None
    sem: float | None
    mean_facility: float | None
    mean_discrimination: float | None
    blueprint_coverage: dict[str, Any]
    item_statistics: list[ItemStatistics] = field(default_factory=list)

    @property
    def is_defensible(self) -> bool | None:
        """Whether reliability supports a pass/fail decision on this paper."""
        reliability = self.cronbach_alpha if self.kr20 is None else self.kr20
        if reliability is None:
            return None
        return reliability >= MINIMUM_DEFENSIBLE_RELIABILITY

    @property
    def flagged_items(self) -> list[dict[str, Any]]:
        return [
            {
                "question_id": item.question_id,
                "facility": item.facility,
                "discrimination": item.discrimination,
                "flags": item.flags,
            }
            for item in self.item_statistics
            if item.flags
        ]


def _flags_for(
    facility: float | None,
    discrimination: float | None,
    distractors: dict[str, dict[str, Any]],
) -> list[str]:
    flags: list[str] = []
    if discrimination is not None:
        if discrimination < NEGATIVE_DISCRIMINATION:
            flags.append("negative_discrimination")
        elif discrimination < POOR_DISCRIMINATION:
            flags.append("poor_discrimination")
    if facility is not None:
        if facility >= TOO_EASY:
            flags.append("too_easy")
        elif facility <= TOO_HARD:
            flags.append("too_hard")
    for key, stats in distractors.items():
        for flag in stats["flags"]:
            flags.append(f"{flag}:{key}")
    return flags


# --------------------------------------------------------------------------
# Database-backed analysis
# --------------------------------------------------------------------------
def analyse_paper(
    db: Session,
    paper: ExamPaper,
    *,
    persist: bool = False,
) -> PaperStatistics | None:
    """Compute item and paper statistics across every marked attempt at a paper.

    Returns ``None`` when fewer than two candidates have submitted, because
    every statistic in this module is undefined for one candidate and inventing
    a number there would be worse than saying nothing.
    """
    attempts = list(
        db.execute(
            select(ExamAttempt)
            .where(
                ExamAttempt.paper_id == paper.id,
                ExamAttempt.status.in_([AttemptStatus.SUBMITTED, AttemptStatus.MARKED]),
            )
            .order_by(ExamAttempt.created_at)
        ).scalars()
    )
    if len(attempts) < 2:
        return None

    attempt_ids = [attempt.id for attempt in attempts]
    responses = list(
        db.execute(
            select(ExamResponse).where(ExamResponse.attempt_id.in_(attempt_ids))
        ).scalars()
    )
    if not responses:
        return None

    by_attempt: dict[str, dict[str, ExamResponse]] = {aid: {} for aid in attempt_ids}
    for response in responses:
        by_attempt[response.attempt_id][response.question_id] = response

    # Only items *every* candidate saw can enter the reliability calculation.
    # With randomised delivery from a large pool that can be a small subset;
    # reporting reliability over a ragged matrix would be meaningless.
    common_ids: set[str] | None = None
    for served in by_attempt.values():
        ids = set(served)
        common_ids = ids if common_ids is None else (common_ids & ids)
    common = sorted(common_ids or set())

    questions = {
        q.id: q
        for q in db.execute(
            select(Question).where(Question.id.in_(common))
        ).scalars()
        if common
    }

    matrix: list[list[float]] = []
    for aid in attempt_ids:
        served = by_attempt[aid]
        matrix.append([served[qid].marks_awarded for qid in common])

    totals = [sum(row) for row in matrix]
    max_total = sum(questions[qid].marks for qid in common) if common else 0.0
    percents = (
        [t / max_total * 100 for t in totals] if max_total > 0 else [0.0] * len(totals)
    )
    ordered_percents = sorted(percents)
    mid = len(ordered_percents) // 2
    median = (
        ordered_percents[mid]
        if len(ordered_percents) % 2
        else (ordered_percents[mid - 1] + ordered_percents[mid]) / 2
    )
    passes = sum(1 for p in percents if p >= paper.pass_mark_percent)

    item_stats: list[ItemStatistics] = []
    for index, qid in enumerate(common):
        question = questions[qid]
        column = [row[index] for row in matrix]
        selections = [by_attempt[aid][qid].selected_keys or [] for aid in attempt_ids]
        seconds = [by_attempt[aid][qid].seconds_spent for aid in attempt_ids]
        option_keys = [str(opt.get("key")) for opt in question.options if opt.get("key")]

        facility = facility_index(column, max_mark=question.marks or 1.0)
        pbis = point_biserial(column, totals)
        dindex = discrimination_index(column, totals, max_mark=question.marks or 1.0)
        distractors = distractor_analysis(
            selections, totals, option_keys, question.correct_keys or []
        )
        item_stats.append(
            ItemStatistics(
                question_id=qid,
                candidates=len(attempt_ids),
                facility=None if facility is None else round(facility, 4),
                discrimination=None if pbis is None else round(pbis, 4),
                discrimination_index=None if dindex is None else round(dindex, 4),
                mean_seconds=round(sum(seconds) / len(seconds), 1) if seconds else None,
                distractor_stats=distractors,
                flags=_flags_for(facility, pbis, distractors),
            )
        )

    reliability_kr20 = kr20(matrix) if _is_dichotomous(matrix, questions, common) else None
    alpha = cronbach_alpha(matrix)
    sem_marks = standard_error_of_measurement(matrix, alpha if reliability_kr20 is None else reliability_kr20)
    sem_percent = (
        None if sem_marks is None or max_total <= 0 else sem_marks / max_total * 100
    )

    facilities = [i.facility for i in item_stats if i.facility is not None]
    discs = [i.discrimination for i in item_stats if i.discrimination is not None]

    stats = PaperStatistics(
        paper_id=paper.id,
        candidates=len(attempt_ids),
        items=len(common),
        mean_percent=round(sum(percents) / len(percents), 2),
        sd_percent=round(math.sqrt(_variance(percents)), 2),
        median_percent=round(median, 2),
        pass_rate=round(passes / len(percents) * 100, 2),
        kr20=None if reliability_kr20 is None else round(reliability_kr20, 4),
        cronbach_alpha=None if alpha is None else round(alpha, 4),
        sem=None if sem_percent is None else round(sem_percent, 2),
        mean_facility=round(sum(facilities) / len(facilities), 4) if facilities else None,
        mean_discrimination=round(sum(discs) / len(discs), 4) if discs else None,
        blueprint_coverage=blueprint_coverage(
            [questions[qid] for qid in common], paper.blueprint_profile or {}
        ),
        item_statistics=item_stats,
    )

    if persist:
        _persist(db, paper, stats)
    return stats


def _is_dichotomous(
    matrix: list[list[float]], questions: dict[str, Question], order: list[str]
) -> bool:
    """Whether every observed mark is either zero or the item's full value."""
    for row in matrix:
        for value, qid in zip(row, order, strict=True):
            full = questions[qid].marks or 1.0
            if value not in (0.0, full):
                return False
    return True


def blueprint_coverage(
    questions: list[Question], requested: dict[str, Any]
) -> dict[str, Any]:
    """Requested against delivered proportions, per blueprint category.

    Reported for every paper whether or not it declared a blueprint: a paper
    with no blueprint still has a distribution, and an examinations officer
    asking "how much of this was basic sciences?" deserves an answer.
    """
    total = len(questions)
    delivered: dict[str, int] = {}
    for question in questions:
        key = question.blueprint_category or "unclassified"
        delivered[key] = delivered.get(key, 0) + 1

    categories = sorted(set(delivered) | set(requested))
    out: dict[str, Any] = {"total_items": total, "categories": {}}
    for category in categories:
        want = float(requested.get(category, 0.0))
        got = delivered.get(category, 0) / total if total else 0.0
        out["categories"][category] = {
            "requested_share": round(want, 4),
            "delivered_share": round(got, 4),
            "delivered_count": delivered.get(category, 0),
            "variance": round(got - want, 4),
        }
    out["unclassified_count"] = delivered.get("unclassified", 0)
    return out


def _persist(db: Session, paper: ExamPaper, stats: PaperStatistics) -> None:
    """Write analyses, replacing any earlier run for this paper.

    Item analyses are upserted on (paper, question); paper analyses accumulate,
    because the whole point of recomputing after a resit is being able to see
    that the statistics changed.
    """
    now = utcnow()
    existing = {
        row.question_id: row
        for row in db.execute(
            select(ItemAnalysis).where(ItemAnalysis.paper_id == paper.id)
        ).scalars()
    }
    for item in stats.item_statistics:
        row = existing.get(item.question_id)
        if row is None:
            row = ItemAnalysis(
                tenant_id=paper.tenant_id,
                paper_id=paper.id,
                question_id=item.question_id,
                computed_at=now,
            )
            db.add(row)
        row.computed_at = now
        row.candidates = item.candidates
        row.facility = item.facility
        row.discrimination = item.discrimination
        row.discrimination_index = item.discrimination_index
        row.mean_seconds = item.mean_seconds
        row.distractor_stats = item.distractor_stats
        row.flags = item.flags

    db.add(
        PaperAnalysis(
            tenant_id=paper.tenant_id,
            paper_id=paper.id,
            computed_at=now,
            candidates=stats.candidates,
            items=stats.items,
            mean_percent=stats.mean_percent,
            sd_percent=stats.sd_percent,
            median_percent=stats.median_percent,
            pass_rate=stats.pass_rate,
            kr20=stats.kr20,
            cronbach_alpha=stats.cronbach_alpha,
            sem=stats.sem,
            mean_facility=stats.mean_facility,
            mean_discrimination=stats.mean_discrimination,
            blueprint_coverage=stats.blueprint_coverage,
            flagged_items=stats.flagged_items,
        )
    )
    db.flush()


def refresh_question_statistics(db: Session, stats: PaperStatistics) -> int:
    """Fold this cohort's results back into each item's live estimates.

    Uses the running ``times_served``/``times_correct`` counters rather than
    overwriting, so an item's difficulty reflects everyone who has ever seen it
    and not just the most recent sitting. Returns the number of items updated.
    """
    updated = 0
    for item in stats.item_statistics:
        question = db.get(Question, item.question_id)
        if question is None:
            continue
        if item.facility is not None:
            question.difficulty = round(1.0 - item.facility, 4)
            question.difficulty_band = band_for_facility(item.facility)
        if item.discrimination is not None:
            question.discrimination = item.discrimination
        question.distractor_stats = item.distractor_stats
        updated += 1
    db.flush()
    return updated
