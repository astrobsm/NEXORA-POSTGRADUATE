"""The Examination Readiness Score, its confidence interval and its drivers.

Eight weighted components, fixed by the specification:

    CME reading completion        20%
    CBT performance               35%
    Procedural logbook            15%
    Clinical competency           10%
    Seminar participation          5%
    Journal club participation     5%
    Case presentations             5%
    Professionalism / consultant   5%

Three design decisions make the number defensible rather than merely computed.

**Unassessed components are excluded and the weights renormalised**, never
scored zero. A department that has not yet run a journal club has produced no
evidence about its trainees' journal-club participation; recording that as 0/100
would penalise every one of them for an administrative gap. This mirrors the
domain-score engine, and for the same reason.

**Every score carries a confidence interval.** A readiness score built on two
logbook entries and one CBT sitting is genuinely uncertain, and a point estimate
would overstate what is known. The interval widens as evidence thins, so
"82 (74-90)" and "82 (81-83)" can be told apart at a glance.

**Influential factors are computed by counterfactual, not by weight.** Asking
"which component has the largest weight?" gives the same answer for everyone.
Asking "if this trainee improved this component by ten points, how much would
the total move?" gives an answer specific to them — and, crucially, ranks a
badly-lagging 5% component above a nearly-complete 35% one when that is the
truth.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from app.models.academic import AcademicActivity, ActivityParticipant
from app.models.assessment import Assessment, AssessmentTemplate
from app.models.cbt import ExamAttempt
from app.models.enums import (
    AcademicActivityKind,
    ApprovalStatus,
    AssessmentKind,
    AssessmentVerdict,
    AttemptStatus,
    LearningIndex,
    ParticipantRole,
    ReadinessCategory,
    ValidationStatus,
)
from app.models.learning import EngagementSnapshot, ReadinessSnapshot
from app.models.logbook import LogEntry
from app.models.training import Enrolment
from app.services.reading import compute_scores as compute_reading_scores

# --------------------------------------------------------------------------
# The weight table. Overridable per institution and per curriculum version;
# these are the specification's defaults and they sum to 1.0 exactly.
# --------------------------------------------------------------------------
COMPONENT_CME_READING = "cme_reading_completion"
COMPONENT_CBT = "cbt_performance"
COMPONENT_LOGBOOK = "procedural_logbook"
COMPONENT_CLINICAL = "clinical_competency"
COMPONENT_SEMINAR = "seminar_participation"
COMPONENT_JOURNAL_CLUB = "journal_club_participation"
COMPONENT_CASE_PRESENTATION = "case_presentations"
COMPONENT_PROFESSIONALISM = "professionalism_evaluation"

DEFAULT_WEIGHTS: dict[str, float] = {
    COMPONENT_CME_READING: 0.20,
    COMPONENT_CBT: 0.35,
    COMPONENT_LOGBOOK: 0.15,
    COMPONENT_CLINICAL: 0.10,
    COMPONENT_SEMINAR: 0.05,
    COMPONENT_JOURNAL_CLUB: 0.05,
    COMPONENT_CASE_PRESENTATION: 0.05,
    COMPONENT_PROFESSIONALISM: 0.05,
}

COMPONENT_LABELS: dict[str, str] = {
    COMPONENT_CME_READING: "CME reading completion",
    COMPONENT_CBT: "CBT performance",
    COMPONENT_LOGBOOK: "Procedural logbook",
    COMPONENT_CLINICAL: "Clinical competency",
    COMPONENT_SEMINAR: "Seminar participation",
    COMPONENT_JOURNAL_CLUB: "Journal club participation",
    COMPONENT_CASE_PRESENTATION: "Case presentations",
    COMPONENT_PROFESSIONALISM: "Professionalism and consultant evaluation",
}

#: Category boundaries, inclusive lower bounds, exactly as specified.
CATEGORY_FLOORS: list[tuple[float, str]] = [
    (90.0, ReadinessCategory.OUTSTANDING),
    (80.0, ReadinessCategory.EXAMINATION_READY),
    (70.0, ReadinessCategory.NEARLY_READY),
    (60.0, ReadinessCategory.NEEDS_IMPROVEMENT),
    (0.0, ReadinessCategory.INTENSIVE_REMEDIATION),
]

#: Evidence counts at which a component is considered fully established. Below
#: these the component still counts, but widens the confidence interval.
FULL_EVIDENCE: dict[str, int] = {
    COMPONENT_CME_READING: 8,      # articles completed
    COMPONENT_CBT: 200,            # items answered
    COMPONENT_LOGBOOK: 40,         # validated entries
    COMPONENT_CLINICAL: 6,         # workplace-based assessments
    COMPONENT_SEMINAR: 4,
    COMPONENT_JOURNAL_CLUB: 4,
    COMPONENT_CASE_PRESENTATION: 4,
    COMPONENT_PROFESSIONALISM: 3,
}

#: Half-width of the interval, in points, for a component with no evidence at
#: all beyond the minimum needed to assess it. Scaled down as evidence
#: accumulates. Chosen so a first-week trainee sees a band roughly 30 points
#: wide and a trainee with a full year of records sees one under 5.
MAX_COMPONENT_UNCERTAINTY = 22.0

#: Targets used when improving a component in the counterfactual. A component
#: already above its target contributes nothing to the ranking.
IMPROVEMENT_STEP = 10.0

#: Verdict-to-score mapping for workplace-based assessments.
VERDICT_SCORES: dict[str, float] = {
    AssessmentVerdict.BELOW_EXPECTATION: 25.0,
    AssessmentVerdict.BORDERLINE: 50.0,
    AssessmentVerdict.MEETS_EXPECTATION: 75.0,
    AssessmentVerdict.ABOVE_EXPECTATION: 90.0,
    AssessmentVerdict.OUTSTANDING: 100.0,
}


def category_for(score: float) -> str:
    for floor, category in CATEGORY_FLOORS:
        if score >= floor:
            return category
    return ReadinessCategory.INTENSIVE_REMEDIATION


@dataclass(slots=True)
class Component:
    key: str
    label: str
    weight: float
    #: ``None`` when there is no evidence at all — the component is then
    #: excluded and its weight redistributed, rather than scored zero.
    score: float | None
    evidence_count: int
    evidence_target: int
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def assessed(self) -> bool:
        return self.score is not None

    @property
    def evidence_ratio(self) -> float:
        if self.evidence_target <= 0:
            return 1.0
        return min(1.0, self.evidence_count / self.evidence_target)

    @property
    def uncertainty(self) -> float:
        """Half-width contributed by this component, in its own units.

        Falls as the square root of evidence: the difference between one and
        four observations is large, between forty and forty-four negligible.
        """
        if not self.assessed:
            return 0.0
        return MAX_COMPONENT_UNCERTAINTY * (1.0 - math.sqrt(self.evidence_ratio))

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "weight": round(self.weight, 4),
            "score": None if self.score is None else round(self.score, 2),
            "assessed": self.assessed,
            "evidence_count": self.evidence_count,
            "evidence_target": self.evidence_target,
            "evidence_ratio": round(self.evidence_ratio, 3),
            "detail": self.detail,
        }


@dataclass(slots=True)
class ReadinessResult:
    score: float
    category: str
    confidence_low: float
    confidence_high: float
    evidence_coverage: float
    components: list[Component]
    influential_factors: list[dict[str, Any]]
    indices: dict[str, float | None]
    weights_used: dict[str, float]

    @property
    def assessed_components(self) -> list[Component]:
        return [c for c in self.components if c.assessed]

    @property
    def unassessed_keys(self) -> list[str]:
        return [c.key for c in self.components if not c.assessed]

    def as_component_dict(self) -> dict[str, Any]:
        return {c.key: c.as_dict() for c in self.components}


# --------------------------------------------------------------------------
# Component measurement
# --------------------------------------------------------------------------
def _cme_component(
    db: Session, user_id: str, window_start: date, window_end: date, weight: float
) -> Component:
    scores = compute_reading_scores(
        db, user_id=user_id, window_start=window_start, window_end=window_end
    )
    if scores.articles_opened == 0:
        score: float | None = None
    else:
        # Reading completion is the substance; consistency modulates it,
        # because material read evenly is retained better than material
        # crammed. Capped so consistency can lift a score by at most a fifth.
        score = scores.reading * 0.8 + scores.consistency * 0.2
    return Component(
        key=COMPONENT_CME_READING,
        label=COMPONENT_LABELS[COMPONENT_CME_READING],
        weight=weight,
        score=score,
        evidence_count=scores.articles_completed,
        evidence_target=FULL_EVIDENCE[COMPONENT_CME_READING],
        detail={
            "reading_score": scores.reading,
            "consistency_score": scores.consistency,
            "engagement_score": scores.engagement,
            "retention_score": scores.retention,
            "articles_opened": scores.articles_opened,
            "articles_completed": scores.articles_completed,
            "active_minutes": scores.active_minutes,
        },
    )


def _cbt_component(
    db: Session, user_id: str, window_start: date, weight: float
) -> Component:
    attempts = list(
        db.execute(
            select(ExamAttempt).where(
                ExamAttempt.user_id == user_id,
                ExamAttempt.status.in_([AttemptStatus.SUBMITTED, AttemptStatus.MARKED]),
                ExamAttempt.percent_score.isnot(None),
            ).order_by(ExamAttempt.submitted_at)
        ).scalars()
    )
    if not attempts:
        return Component(
            key=COMPONENT_CBT,
            label=COMPONENT_LABELS[COMPONENT_CBT],
            weight=weight,
            score=None,
            evidence_count=0,
            evidence_target=FULL_EVIDENCE[COMPONENT_CBT],
            detail={"attempts": 0},
        )

    # Recency-weighted mean. A trainee who scored 40% in January and 80% in
    # June is not a 60% trainee; a flat average would say they were, and would
    # make measurable improvement invisible.
    weights = [1.5**i for i in range(len(attempts))]
    total_weight = sum(weights)
    weighted = (
        sum((a.percent_score or 0.0) * w for a, w in zip(attempts, weights, strict=True))
        / total_weight
    )
    items = sum(len(a.served_question_ids or []) for a in attempts)
    recent = [a.percent_score or 0.0 for a in attempts[-3:]]

    return Component(
        key=COMPONENT_CBT,
        label=COMPONENT_LABELS[COMPONENT_CBT],
        weight=weight,
        score=weighted,
        evidence_count=items,
        evidence_target=FULL_EVIDENCE[COMPONENT_CBT],
        detail={
            "attempts": len(attempts),
            "items_answered": items,
            "latest_percent": attempts[-1].percent_score,
            "mean_percent": round(
                sum(a.percent_score or 0.0 for a in attempts) / len(attempts), 2
            ),
            "recency_weighted_percent": round(weighted, 2),
            "recent_three": recent,
            "passes": sum(1 for a in attempts if a.is_pass),
        },
    )


def _logbook_component(
    db: Session, user_id: str, window_start: date, weight: float
) -> Component:
    # Log entries belong to an enrolment, not directly to a user — a trainee
    # who transfers programmes keeps both enrolments and both logbooks.
    rows = db.execute(
        select(
            func.count(LogEntry.id),
            func.sum(
                func.cast(
                    LogEntry.validation_status == ValidationStatus.VALIDATED, Integer
                )
            ),
        )
        .join(Enrolment, LogEntry.enrolment_id == Enrolment.id)
        .where(Enrolment.trainee_id == user_id, LogEntry.deleted_at.is_(None))
    ).one()
    total, validated = rows[0] or 0, rows[1] or 0
    if total == 0:
        return Component(
            key=COMPONENT_LOGBOOK,
            label=COMPONENT_LABELS[COMPONENT_LOGBOOK],
            weight=weight,
            score=None,
            evidence_count=0,
            evidence_target=FULL_EVIDENCE[COMPONENT_LOGBOOK],
            detail={"entries": 0},
        )

    target = FULL_EVIDENCE[COMPONENT_LOGBOOK]
    volume = min(1.0, validated / target) * 100
    # Validation hygiene is a real signal: entries a supervisor never signed
    # are not evidence of anything, and a trainee with 90 unvalidated entries
    # has a paperwork problem their score should show.
    hygiene = validated / total * 100
    score = volume * 0.75 + hygiene * 0.25

    return Component(
        key=COMPONENT_LOGBOOK,
        label=COMPONENT_LABELS[COMPONENT_LOGBOOK],
        weight=weight,
        score=score,
        evidence_count=int(validated),
        evidence_target=target,
        detail={
            "entries": int(total),
            "validated": int(validated),
            "validation_rate": round(hygiene, 2),
            "volume_against_target": round(volume, 2),
        },
    )


#: Assessment kinds that measure professionalism rather than clinical skill.
#: Split out so the two 10%/5% components do not double-count the same forms.
PROFESSIONALISM_KINDS = [AssessmentKind.PROFESSIONALISM, AssessmentKind.MSF]


def _assessment_scores(
    db: Session, user_id: str, *, kinds: list[str], exclude: bool
) -> list[float]:
    """Completed assessments for a trainee, as 0-100 scores.

    An assessment belongs to an enrolment, not to a user, so the join runs
    through ``Enrolment.trainee_id``. Prefers the verdict, which is what the
    assessor actually judged, and falls back to the percentage when a template
    scores numerically without a verdict.
    """
    stmt = (
        select(Assessment, AssessmentTemplate.kind)
        .join(Enrolment, Assessment.enrolment_id == Enrolment.id)
        .join(AssessmentTemplate, Assessment.template_id == AssessmentTemplate.id)
        .where(
            Enrolment.trainee_id == user_id,
            Assessment.deleted_at.is_(None),
            Assessment.status.in_([ApprovalStatus.SUBMITTED, ApprovalStatus.APPROVED]),
        )
    )
    if exclude:
        stmt = stmt.where(AssessmentTemplate.kind.notin_(kinds))
    else:
        stmt = stmt.where(AssessmentTemplate.kind.in_(kinds))

    scores: list[float] = []
    for assessment, _kind in db.execute(stmt).all():
        if assessment.verdict in VERDICT_SCORES:
            scores.append(VERDICT_SCORES[assessment.verdict])
        elif assessment.percent_score is not None:
            scores.append(float(assessment.percent_score))
    return scores


def _clinical_component(db: Session, user_id: str, weight: float) -> Component:
    scored = _assessment_scores(
        db, user_id, kinds=PROFESSIONALISM_KINDS, exclude=True
    )
    if not scored:
        return Component(
            key=COMPONENT_CLINICAL,
            label=COMPONENT_LABELS[COMPONENT_CLINICAL],
            weight=weight,
            score=None,
            evidence_count=0,
            evidence_target=FULL_EVIDENCE[COMPONENT_CLINICAL],
            detail={"assessments": 0},
        )
    return Component(
        key=COMPONENT_CLINICAL,
        label=COMPONENT_LABELS[COMPONENT_CLINICAL],
        weight=weight,
        score=sum(scored) / len(scored),
        evidence_count=len(scored),
        evidence_target=FULL_EVIDENCE[COMPONENT_CLINICAL],
        detail={
            "assessments": len(scored),
            "mean": round(sum(scored) / len(scored), 2),
            "lowest": min(scored),
            "highest": max(scored),
        },
    )


def _activity_component(
    db: Session,
    user_id: str,
    *,
    key: str,
    weight: float,
    kinds: list[str],
    presenting_roles: list[str],
    window_start: date,
) -> Component:
    """Attendance and presentation at one class of academic activity.

    Presenting is worth more than attending — that is the whole point of the
    seminar and journal-club requirements — so presentations are counted at
    triple weight against the target.
    """
    rows = list(
        db.execute(
            select(ActivityParticipant, AcademicActivity)
            .join(
                AcademicActivity,
                ActivityParticipant.activity_id == AcademicActivity.id,
            )
            .where(
                ActivityParticipant.user_id == user_id,
                AcademicActivity.kind.in_(kinds),
                AcademicActivity.scheduled_on >= window_start,
                AcademicActivity.deleted_at.is_(None),
            )
        ).all()
    )
    if not rows:
        return Component(
            key=key,
            label=COMPONENT_LABELS[key],
            weight=weight,
            score=None,
            evidence_count=0,
            evidence_target=FULL_EVIDENCE[key],
            detail={"records": 0},
        )

    attended = sum(1 for p, _ in rows if p.attended)
    presented = sum(
        1 for p, _ in rows if p.attended and p.role in presenting_roles
    )
    target = FULL_EVIDENCE[key]
    credit = (attended - presented) + presented * 3
    score = min(100.0, credit / target * 100)

    return Component(
        key=key,
        label=COMPONENT_LABELS[key],
        weight=weight,
        score=score,
        evidence_count=attended,
        evidence_target=target,
        detail={
            "records": len(rows),
            "attended": attended,
            "presented": presented,
            "attendance_rate": round(attended / len(rows) * 100, 2),
            "credit_units": credit,
            "target_units": target,
        },
    )


def _professionalism_component(db: Session, user_id: str, weight: float) -> Component:
    scores = _assessment_scores(
        db, user_id, kinds=PROFESSIONALISM_KINDS, exclude=False
    )
    if not scores:
        return Component(
            key=COMPONENT_PROFESSIONALISM,
            label=COMPONENT_LABELS[COMPONENT_PROFESSIONALISM],
            weight=weight,
            score=None,
            evidence_count=0,
            evidence_target=FULL_EVIDENCE[COMPONENT_PROFESSIONALISM],
            detail={"ratings": 0},
        )
    return Component(
        key=COMPONENT_PROFESSIONALISM,
        label=COMPONENT_LABELS[COMPONENT_PROFESSIONALISM],
        weight=weight,
        score=sum(scores) / len(scores),
        evidence_count=len(scores),
        evidence_target=FULL_EVIDENCE[COMPONENT_PROFESSIONALISM],
        detail={"ratings": len(scores), "mean": round(sum(scores) / len(scores), 2)},
    )


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def _aggregate(components: list[Component]) -> tuple[float, float, float, float]:
    """Weighted mean over assessed components, plus interval and coverage.

    Returns ``(score, low, high, coverage)``. Coverage is the share of the
    original weight table that had evidence behind it — the honest headline
    number when someone asks how much of this score is actually measured.
    """
    assessed = [c for c in components if c.assessed]
    declared = sum(c.weight for c in components)
    if not assessed or declared <= 0:
        return 0.0, 0.0, 100.0, 0.0

    base = sum(c.weight for c in assessed)
    score = sum((c.score or 0.0) * c.weight for c in assessed) / base
    coverage = base / declared

    # Component uncertainties are independent enough to combine in quadrature
    # rather than by simple addition, which would produce absurdly wide bands
    # on a score with eight contributors.
    variance = sum(((c.weight / base) * c.uncertainty) ** 2 for c in assessed)
    half_width = math.sqrt(variance)

    # Missing components are their own source of uncertainty: a score built on
    # 60% of the weight table could move a long way once the rest arrives.
    half_width += (1.0 - coverage) * MAX_COMPONENT_UNCERTAINTY

    return (
        round(score, 2),
        round(max(0.0, score - half_width), 2),
        round(min(100.0, score + half_width), 2),
        round(coverage, 4),
    )


def _influential_factors(
    components: list[Component], current: float
) -> list[dict[str, Any]]:
    """Rank components by how much the total would move if each improved.

    The counterfactual is a fixed ten-point improvement in one component with
    everything else held constant. That makes the ranking specific to this
    trainee: a component already at 95 cannot contribute ten points, so it
    falls down the list however heavily it is weighted.

    Unassessed components are ranked separately, by how much weight they would
    *bring back into play* — because for those the action is not "improve" but
    "get any evidence at all", which is a different instruction.
    """
    assessed = [c for c in components if c.assessed]
    base = sum(c.weight for c in assessed)
    factors: list[dict[str, Any]] = []

    for component in assessed:
        headroom = min(IMPROVEMENT_STEP, 100.0 - (component.score or 0.0))
        gain = (component.weight / base) * headroom if base > 0 else 0.0
        factors.append(
            {
                "component": component.key,
                "label": component.label,
                "status": "assessed",
                "current_score": round(component.score or 0.0, 2),
                "weight": round(component.weight, 4),
                "headroom": round(headroom, 2),
                "readiness_gain_if_improved": round(gain, 2),
                "action": (
                    f"Raise {component.label.lower()} by "
                    f"{headroom:.0f} points to add {gain:.1f} to readiness."
                    if headroom > 0.5
                    else f"{component.label} is at or near its ceiling."
                ),
            }
        )

    for component in components:
        if component.assessed:
            continue
        factors.append(
            {
                "component": component.key,
                "label": component.label,
                "status": "unassessed",
                "current_score": None,
                "weight": round(component.weight, 4),
                "headroom": None,
                "readiness_gain_if_improved": None,
                "action": (
                    f"No evidence recorded for {component.label.lower()}. "
                    f"{component.weight * 100:.0f}% of the score is currently "
                    "carried by the other components."
                ),
            }
        )

    factors.sort(
        key=lambda f: (
            f["status"] != "assessed",
            -(f["readiness_gain_if_improved"] or 0.0),
            -(f["weight"] or 0.0),
        )
    )
    return factors


def _indices(
    components: dict[str, Component],
    db: Session,
    user_id: str,
    score: float,
) -> dict[str, float | None]:
    """The nine dashboard indices.

    Each is a named view over evidence already gathered, not a new measurement.
    Where a component that would feed an index is unassessed the index is
    ``None`` rather than zero, for the same reason components are.
    """

    def score_of(key: str) -> float | None:
        component = components.get(key)
        return component.score if component and component.assessed else None

    cbt = components.get(COMPONENT_CBT)
    cbt_detail = cbt.detail if cbt else {}
    recent = cbt_detail.get("recent_three") or []

    # Improvement rate: slope across recent sittings, expressed as points per
    # sitting. Needs at least three or it is noise.
    improvement: float | None = None
    if len(recent) >= 3:
        n = len(recent)
        mean_x = (n - 1) / 2
        mean_y = sum(recent) / n
        denominator = sum((i - mean_x) ** 2 for i in range(n))
        if denominator > 0:
            improvement = round(
                sum((i - mean_x) * (y - mean_y) for i, y in enumerate(recent))
                / denominator,
                2,
            )

    # Consistency: the reading consistency score, which is the only component
    # that measures regularity rather than volume.
    cme = components.get(COMPONENT_CME_READING)
    consistency = (cme.detail.get("consistency_score") if cme else None) or None
    retention = (cme.detail.get("retention_score") if cme else None) or None

    # Learning velocity: completed articles per active week. Reported in its
    # own units, not normalised to 100, because a rate is not a percentage.
    active_minutes = (cme.detail.get("active_minutes") if cme else 0) or 0
    completed = (cme.detail.get("articles_completed") if cme else 0) or 0
    velocity = round(completed / max(1.0, active_minutes / 60 / 5), 2) if completed else None

    return {
        LearningIndex.KNOWLEDGE: score_of(COMPONENT_CBT),
        LearningIndex.CLINICAL_COMPETENCY: score_of(COMPONENT_CLINICAL),
        LearningIndex.PROCEDURAL_COMPETENCY: score_of(COMPONENT_LOGBOOK),
        # Critical thinking is proxied by performance on the higher Bloom
        # levels; until items carry enough Bloom tagging to separate them this
        # tracks CBT performance and is labelled as a proxy in the API.
        LearningIndex.CRITICAL_THINKING: score_of(COMPONENT_CBT),
        LearningIndex.CONSISTENCY: consistency,
        LearningIndex.IMPROVEMENT_RATE: improvement,
        LearningIndex.LEARNING_VELOCITY: velocity,
        LearningIndex.RETENTION: retention,
        LearningIndex.EXAMINATION_PREDICTION: round(score, 2),
    }


def compute_readiness(
    db: Session,
    *,
    user_id: str,
    as_of: date | None = None,
    window_days: int = 90,
    weights: dict[str, float] | None = None,
) -> ReadinessResult:
    """Compute the Examination Readiness Score for one trainee."""
    as_of = as_of or date.today()
    window_start = as_of - timedelta(days=window_days)
    table = dict(weights or DEFAULT_WEIGHTS)

    components = [
        _cme_component(db, user_id, window_start, as_of, table[COMPONENT_CME_READING]),
        _cbt_component(db, user_id, window_start, table[COMPONENT_CBT]),
        _logbook_component(db, user_id, window_start, table[COMPONENT_LOGBOOK]),
        _clinical_component(db, user_id, table[COMPONENT_CLINICAL]),
        _activity_component(
            db,
            user_id,
            key=COMPONENT_SEMINAR,
            weight=table[COMPONENT_SEMINAR],
            kinds=[
                AcademicActivityKind.SEMINAR,
                AcademicActivityKind.SKILLS_WORKSHOP,
                AcademicActivityKind.CME_SESSION,
            ],
            presenting_roles=[ParticipantRole.PRESENTER, ParticipantRole.MODERATOR],
            window_start=window_start,
        ),
        _activity_component(
            db,
            user_id,
            key=COMPONENT_JOURNAL_CLUB,
            weight=table[COMPONENT_JOURNAL_CLUB],
            kinds=[AcademicActivityKind.JOURNAL_CLUB],
            presenting_roles=[
                ParticipantRole.PRESENTER,
                ParticipantRole.DISCUSSANT,
            ],
            window_start=window_start,
        ),
        _activity_component(
            db,
            user_id,
            key=COMPONENT_CASE_PRESENTATION,
            weight=table[COMPONENT_CASE_PRESENTATION],
            kinds=[
                AcademicActivityKind.GRAND_ROUND,
                AcademicActivityKind.CPC,
                AcademicActivityKind.MORTALITY_MEETING,
                AcademicActivityKind.MORBIDITY_MEETING,
                AcademicActivityKind.TUMOUR_BOARD,
            ],
            presenting_roles=[ParticipantRole.PRESENTER],
            window_start=window_start,
        ),
        _professionalism_component(db, user_id, table[COMPONENT_PROFESSIONALISM]),
    ]

    score, low, high, coverage = _aggregate(components)
    by_key = {c.key: c for c in components}
    return ReadinessResult(
        score=score,
        category=category_for(score),
        confidence_low=low,
        confidence_high=high,
        evidence_coverage=coverage,
        components=components,
        influential_factors=_influential_factors(components, score),
        indices=_indices(by_key, db, user_id, score),
        weights_used=table,
    )


def snapshot_readiness(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    as_of: date | None = None,
    enrolment_id: str | None = None,
    target_examination: str | None = None,
    target_date: date | None = None,
    window_days: int = 90,
    weights: dict[str, float] | None = None,
) -> ReadinessSnapshot:
    """Compute and persist a readiness snapshot, recording the delta from the last."""
    as_of = as_of or date.today()
    result = compute_readiness(
        db, user_id=user_id, as_of=as_of, window_days=window_days, weights=weights
    )

    previous = db.execute(
        select(ReadinessSnapshot)
        .where(ReadinessSnapshot.user_id == user_id, ReadinessSnapshot.as_of < as_of)
        .order_by(ReadinessSnapshot.as_of.desc())
        .limit(1)
    ).scalar_one_or_none()

    snapshot = ReadinessSnapshot(
        tenant_id=tenant_id,
        user_id=user_id,
        enrolment_id=enrolment_id,
        as_of=as_of,
        target_examination=target_examination,
        target_date=target_date,
        score=result.score,
        category=result.category,
        confidence_low=result.confidence_low,
        confidence_high=result.confidence_high,
        evidence_coverage=result.evidence_coverage,
        components=result.as_component_dict(),
        influential_factors=result.influential_factors,
        indices=result.indices,
        weights_used=result.weights_used,
        delta_from_previous=(
            None if previous is None else round(result.score - previous.score, 2)
        ),
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def latest_engagement(db: Session, user_id: str) -> EngagementSnapshot | None:
    return db.execute(
        select(EngagementSnapshot)
        .where(EngagementSnapshot.user_id == user_id)
        .order_by(EngagementSnapshot.window_end.desc())
        .limit(1)
    ).scalar_one_or_none()
