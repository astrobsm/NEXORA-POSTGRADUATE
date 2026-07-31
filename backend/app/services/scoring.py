"""Performance analytics: the eight domain scores, RAG status and the overall score.

Scores are derived from two sources and nothing else:

1. **Requirement results** — every rule that declares a ``score_domain`` contributes its
   ``progress_percent``, weighted by ``weight``. This is what makes the score defensible:
   it is literally the curriculum, measured.
2. **Direct signals** that no rule captures — assessment means, professionalism markers,
   logbook validation hygiene — folded in at institution-configurable weights.

Every snapshot records the weights it used, so a score computed today can still be
explained after the curriculum changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.analytics import ScoreSnapshot
from app.models.assessment import Assessment
from app.models.cbt import ExamAttempt
from app.models.curriculum import CurriculumVersion
from app.models.enums import (
    RagStatus,
    RequirementScope,
    RequirementSeverity,
    ScoreDomain,
    ValidationStatus,
)
from app.models.logbook import LogEntry
from app.models.training import Enrolment
from app.services.requirements import (
    EvaluationContext,
    RequirementResult,
    evaluate_many,
    load_rules,
)

# --------------------------------------------------------------------------
# Defaults — every one of these is overridable per curriculum version.
# --------------------------------------------------------------------------
DEFAULT_WEIGHTS: dict[str, float] = {
    ScoreDomain.CLINICAL_COMPETENCY: 0.28,
    ScoreDomain.ACADEMIC: 0.14,
    ScoreDomain.ATTENDANCE: 0.12,
    ScoreDomain.RESEARCH: 0.14,
    ScoreDomain.PROFESSIONALISM: 0.12,
    ScoreDomain.TEACHING: 0.08,
    ScoreDomain.LEADERSHIP: 0.05,
    ScoreDomain.EXAM_READINESS: 0.07,
}

#: Score at or above which a domain is green; below the amber floor it is red.
DEFAULT_RAG_THRESHOLDS: dict[str, float] = {"green": 75.0, "amber": 55.0}


@dataclass(slots=True)
class DomainScore:
    domain: str
    score: float
    rag: str
    #: Which requirement rules drove this number.
    contributing_rules: int = 0
    #: Direct (non-rule) signals folded in.
    signals: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScoreReport:
    enrolment_id: str
    computed_at: Any
    training_year: int
    domains: dict[str, DomainScore]
    overall_score: float
    overall_rag: str
    promotion_readiness_score: float
    requirement_results: list[RequirementResult]
    gaps: list[dict[str, Any]]
    metrics: dict[str, Any]
    weights_used: dict[str, float]
    #: Domains the curriculum does not assess; excluded from ``overall_score``.
    unassessed_domains: list[str] = field(default_factory=list)
    #: Sum of the weights that actually contributed, before renormalisation.
    effective_weight_base: float = 1.0

    def domain_score(self, domain: str) -> float:
        entry = self.domains.get(domain)
        return entry.score if entry else 0.0


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def rag_for(score: float, thresholds: dict[str, float] | None = None) -> str:
    t = thresholds or DEFAULT_RAG_THRESHOLDS
    if score >= t.get("green", 75.0):
        return RagStatus.GREEN
    if score >= t.get("amber", 55.0):
        return RagStatus.AMBER
    return RagStatus.RED


def resolve_weights(version: CurriculumVersion | None) -> dict[str, float]:
    """Curriculum weights, normalised to sum to 1.0 so a misconfigured set cannot
    silently inflate or deflate every trainee's overall score."""
    weights = dict(DEFAULT_WEIGHTS)
    if version and version.score_weights:
        for key, value in version.score_weights.items():
            if key in DEFAULT_WEIGHTS and isinstance(value, (int, float)) and value >= 0:
                weights[key] = float(value)
    total = sum(weights.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in weights.items()}


# --------------------------------------------------------------------------
# Direct signals — measurements that are not themselves curriculum requirements
# --------------------------------------------------------------------------
def collect_direct_metrics(db: Session, enrolment: Enrolment, as_of: date) -> dict[str, Any]:
    """Raw counts used by the professionalism and clinical signals, and shown on
    dashboards in their own right."""
    log_rows = db.execute(
        select(LogEntry.validation_status, func.count(), func.sum(LogEntry.query_count))
        .where(LogEntry.enrolment_id == enrolment.id, LogEntry.deleted_at.is_(None))
        .group_by(LogEntry.validation_status)
    ).all()
    by_status = {row[0]: {"count": row[1], "queries": row[2] or 0} for row in log_rows}
    total_logs = sum(v["count"] for v in by_status.values())
    validated = by_status.get(ValidationStatus.VALIDATED, {}).get("count", 0)
    rejected = by_status.get(ValidationStatus.REJECTED, {}).get("count", 0)
    queries = sum(v["queries"] for v in by_status.values())

    assessments = db.execute(
        select(Assessment.percent_score).where(
            Assessment.enrolment_id == enrolment.id,
            Assessment.status == "approved",
            Assessment.percent_score.is_not(None),
            Assessment.deleted_at.is_(None),
        )
    ).scalars().all()

    exam_scores = db.execute(
        select(ExamAttempt.percent_score).where(
            ExamAttempt.enrolment_id == enrolment.id,
            ExamAttempt.status == "marked",
            ExamAttempt.percent_score.is_not(None),
        )
    ).scalars().all()

    return {
        "logbook": {
            "total": total_logs,
            "validated": validated,
            "pending": by_status.get(ValidationStatus.PENDING, {}).get("count", 0),
            "rejected": rejected,
            "queries": queries,
            "validation_rate": (validated / total_logs * 100) if total_logs else 0.0,
        },
        "assessments": {
            "count": len(assessments),
            "mean_percent": (sum(assessments) / len(assessments)) if assessments else None,
        },
        "exams": {
            "attempts": len(exam_scores),
            "mean_percent": (sum(exam_scores) / len(exam_scores)) if exam_scores else None,
            "best_percent": max(exam_scores) if exam_scores else None,
        },
        "as_of": str(as_of),
    }


def professionalism_signal(metrics: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """A conduct proxy built from evidence the platform actually holds.

    Starts at full marks and deducts for logbook entries rejected outright or returned
    repeatedly for correction — both indicators of record-keeping that a college would
    query. Deliberately conservative: it never invents a judgement about character, and
    is presented alongside the counts that produced it.
    """
    logbook = metrics.get("logbook", {})
    total = logbook.get("total", 0)
    if not total:
        return 70.0, {"basis": "no logbook activity yet — neutral baseline applied"}

    rejected_rate = logbook.get("rejected", 0) / total
    query_rate = min(1.0, logbook.get("queries", 0) / total)
    score = 100.0 - (rejected_rate * 60.0) - (query_rate * 25.0)
    return max(0.0, min(100.0, score)), {
        "rejected_rate": round(rejected_rate * 100, 1),
        "query_rate": round(query_rate * 100, 1),
        "validation_rate": round(logbook.get("validation_rate", 0.0), 1),
    }


# --------------------------------------------------------------------------
# Core calculation
# --------------------------------------------------------------------------
def _score_from_rules(results: list[RequirementResult], domain: str) -> tuple[float | None, int]:
    """Weighted mean progress of the rules assigned to a domain."""
    relevant = [r for r in results if r.score_domain == domain]
    if not relevant:
        return None, 0
    total_weight = sum(r.weight for r in relevant) or 1.0
    score = sum(r.progress_percent * r.weight for r in relevant) / total_weight
    return score, len(relevant)


def compute_scores(
    db: Session,
    enrolment: Enrolment,
    *,
    as_of: date | None = None,
    trigger: str = "on_demand",
) -> ScoreReport:
    """Evaluate every applicable requirement and roll it up into the eight domains."""
    as_of = as_of or date.today()
    version = db.get(CurriculumVersion, enrolment.curriculum_version_id)
    weights = resolve_weights(version)
    thresholds = DEFAULT_RAG_THRESHOLDS
    if version and isinstance(version.score_weights, dict):
        thresholds = version.score_weights.get("_rag_thresholds", DEFAULT_RAG_THRESHOLDS)

    rules = load_rules(
        db,
        enrolment.curriculum_version_id,
        scopes=[
            RequirementScope.PROGRAMME,
            RequirementScope.TRAINING_YEAR,
            RequirementScope.PROMOTION,
            RequirementScope.EXAM_ELIGIBILITY,
        ],
        training_year=enrolment.current_year,
    )
    ctx = EvaluationContext(
        db=db, enrolment=enrolment, as_of=as_of, training_year=enrolment.current_year
    )
    results = evaluate_many(ctx, rules)
    metrics = collect_direct_metrics(db, enrolment, as_of)

    domains: dict[str, DomainScore] = {}
    #: Domains with neither a requirement rule nor a direct signal. These are *not
    #: measured*, which is a different thing from measured-as-zero: a curriculum that
    #: never defines a leadership requirement must not thereby score every trainee zero
    #: for leadership. Unassessed domains are excluded from the overall mean and their
    #: weight is redistributed across the domains that do have evidence.
    unassessed: set[str] = set()

    for domain in ScoreDomain:
        rule_score, count = _score_from_rules(results, domain.value)
        signals: dict[str, Any] = {}
        score = rule_score

        if domain == ScoreDomain.PROFESSIONALISM:
            prof_score, prof_detail = professionalism_signal(metrics)
            signals = prof_detail
            score = prof_score if score is None else (score * 0.6 + prof_score * 0.4)

        elif domain == ScoreDomain.CLINICAL_COMPETENCY:
            mean_assessment = metrics["assessments"]["mean_percent"]
            if mean_assessment is not None:
                signals = {"mean_assessment_percent": round(mean_assessment, 1),
                           "assessment_count": metrics["assessments"]["count"]}
                score = mean_assessment if score is None else (score * 0.7 + mean_assessment * 0.3)

        elif domain == ScoreDomain.EXAM_READINESS:
            exam_mean = metrics["exams"]["mean_percent"]
            best = metrics["exams"]["best_percent"]
            if exam_mean is not None:
                # Weight the best attempt slightly — readiness is about peak capability.
                blended = exam_mean * 0.6 + (best or exam_mean) * 0.4
                signals = {"mean_percent": round(exam_mean, 1), "best_percent": round(best or 0, 1)}
                score = blended if score is None else (score * 0.5 + blended * 0.5)

        if score is None:
            unassessed.add(domain.value)
            domains[domain.value] = DomainScore(
                domain=domain.value,
                score=0.0,
                rag=RagStatus.UNKNOWN,
                contributing_rules=0,
                signals={
                    "basis": "not assessed — this curriculum defines no requirement in "
                             "this domain and no other evidence is available",
                    "excluded_from_overall": True,
                },
            )
            continue

        score = max(0.0, min(100.0, score))
        domains[domain.value] = DomainScore(
            domain=domain.value,
            score=score,
            rag=rag_for(score, thresholds),
            contributing_rules=count,
            signals=signals,
        )

    assessed = [d for d in domains if d not in unassessed]
    assessed_weight = sum(weights.get(d, 0.0) for d in assessed)
    if assessed_weight > 0:
        overall = sum(domains[d].score * weights.get(d, 0.0) for d in assessed) / assessed_weight
    else:
        overall = 0.0
    overall = max(0.0, min(100.0, overall))

    # Promotion readiness is a different question from performance: it asks how many of
    # the *mandatory* gates are cleared, not how well the trainee is doing overall.
    mandatory = [r for r in results if r.severity == RequirementSeverity.MANDATORY]
    if mandatory:
        readiness = sum(r.progress_percent for r in mandatory) / len(mandatory)
    else:
        readiness = overall

    gaps = build_gaps(results)

    return ScoreReport(
        enrolment_id=enrolment.id,
        computed_at=utcnow(),
        training_year=enrolment.current_year,
        domains=domains,
        overall_score=overall,
        overall_rag=rag_for(overall, thresholds),
        promotion_readiness_score=readiness,
        requirement_results=results,
        gaps=gaps,
        metrics=metrics,
        weights_used=weights,
        unassessed_domains=sorted(unassessed),
        effective_weight_base=round(assessed_weight, 4),
    )


def build_gaps(results: list[RequirementResult]) -> list[dict[str, Any]]:
    """Unmet requirements, ordered so the most consequential appear first."""
    severity_rank = {
        RequirementSeverity.MANDATORY: 0,
        RequirementSeverity.RECOMMENDED: 1,
        RequirementSeverity.INFORMATIONAL: 2,
    }
    unmet = [r for r in results if not r.met]
    unmet.sort(key=lambda r: (severity_rank.get(r.severity, 3), r.progress_percent))
    return [
        {
            "rule_id": r.rule_id,
            "label": r.label,
            "severity": r.severity,
            "scope": r.scope,
            "domain": r.score_domain,
            "measured": round(r.measured, 2),
            "target": round(r.target, 2),
            "shortfall": round(r.shortfall, 2),
            "progress_percent": round(r.progress_percent, 1),
            "guidance": r.guidance,
        }
        for r in unmet
    ]


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
def persist_snapshot(
    db: Session, enrolment: Enrolment, report: ScoreReport, *, trigger: str = "on_demand"
) -> ScoreSnapshot:
    """Write an immutable snapshot and refresh the enrolment's denormalised fields."""
    snapshot = ScoreSnapshot(
        tenant_id=enrolment.tenant_id,
        enrolment_id=enrolment.id,
        computed_at=report.computed_at,
        training_year=report.training_year,
        trigger=trigger,
        clinical_competency_score=report.domain_score(ScoreDomain.CLINICAL_COMPETENCY),
        research_score=report.domain_score(ScoreDomain.RESEARCH),
        academic_score=report.domain_score(ScoreDomain.ACADEMIC),
        professionalism_score=report.domain_score(ScoreDomain.PROFESSIONALISM),
        leadership_score=report.domain_score(ScoreDomain.LEADERSHIP),
        attendance_score=report.domain_score(ScoreDomain.ATTENDANCE),
        teaching_score=report.domain_score(ScoreDomain.TEACHING),
        exam_readiness_score=report.domain_score(ScoreDomain.EXAM_READINESS),
        overall_score=report.overall_score,
        promotion_readiness_score=report.promotion_readiness_score,
        overall_rag=report.overall_rag,
        domain_rag={d: s.rag for d, s in report.domains.items()},
        requirement_results=[r.to_dict() for r in report.requirement_results],
        gaps=report.gaps,
        metrics=report.metrics,
        weights_used=report.weights_used,
    )
    db.add(snapshot)

    enrolment.latest_overall_score = report.overall_score
    enrolment.latest_rag = report.overall_rag
    enrolment.last_scored_at = report.computed_at
    db.add(enrolment)
    return snapshot


def score_and_persist(
    db: Session, enrolment: Enrolment, *, as_of: date | None = None, trigger: str = "on_demand"
) -> tuple[ScoreReport, ScoreSnapshot]:
    report = compute_scores(db, enrolment, as_of=as_of, trigger=trigger)
    snapshot = persist_snapshot(db, enrolment, report, trigger=trigger)
    return report, snapshot
