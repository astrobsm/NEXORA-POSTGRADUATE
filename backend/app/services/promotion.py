"""The promotion engine.

Determines, without manual calculation, whether a trainee is ready to move
Registrar → Senior Registrar, Senior Registrar → fellowship examination, or House
Officer → completion. The engine *recommends*; a human ratifies. Any human decision
that departs from the recommendation must record a reason, which the audit log keeps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.analytics import PromotionReview, ScoreSnapshot
from app.models.curriculum import CurriculumVersion, TrainingYear
from app.models.enums import (
    EnrolmentStatus,
    ProgrammeType,
    PromotionOutcome,
    RequirementScope,
    RequirementSeverity,
    RotationStatus,
    TrainingLevel,
)
from app.models.training import Enrolment
from app.services.requirements import (
    EvaluationContext,
    RequirementResult,
    evaluate_many,
    load_rules,
)
from app.services.scoring import ScoreReport, compute_scores, persist_snapshot

#: Default progression ladder. A programme may override it in ``Programme.settings``
#: under the key ``level_progression``.
DEFAULT_PROGRESSION: dict[str, str] = {
    TrainingLevel.HOUSE_OFFICER: TrainingLevel.MEDICAL_OFFICER,
    TrainingLevel.INTERN: TrainingLevel.MEDICAL_OFFICER,
    TrainingLevel.MEDICAL_OFFICER: TrainingLevel.REGISTRAR,
    TrainingLevel.REGISTRAR: TrainingLevel.SENIOR_REGISTRAR,
    TrainingLevel.SENIOR_REGISTRAR: TrainingLevel.FELLOW,
    TrainingLevel.FELLOW: TrainingLevel.CONSULTANT_TRAINER,
}


@dataclass(slots=True)
class PromotionAssessment:
    enrolment_id: str
    from_level: str
    to_level: str
    from_year: int
    to_year: int
    outcome: str
    readiness_percent: float
    blocking: list[dict[str, Any]]
    advisories: list[dict[str, Any]]
    rationale: str
    time_served_months: int
    minimum_months_required: int
    checks: dict[str, Any] = field(default_factory=dict)
    score_report: ScoreReport | None = None

    @property
    def is_recommended(self) -> bool:
        return self.outcome == PromotionOutcome.RECOMMENDED


# --------------------------------------------------------------------------
def months_served(enrolment: Enrolment, as_of: date) -> int:
    """Training time actually served, excluding approved interruptions."""
    delta = relativedelta(as_of, enrolment.start_date)
    gross = delta.years * 12 + delta.months
    return max(0, gross - (enrolment.interruption_days // 30))


def next_level(enrolment: Enrolment, programme_settings: dict[str, Any] | None) -> str:
    overrides = (programme_settings or {}).get("level_progression") or {}
    return overrides.get(
        enrolment.current_level,
        DEFAULT_PROGRESSION.get(enrolment.current_level, enrolment.current_level),
    )


def _is_final_year(db: Session, enrolment: Enrolment) -> bool:
    version = db.get(CurriculumVersion, enrolment.curriculum_version_id)
    if not version:
        return False
    years = [y.sequence for y in version.training_years]
    return bool(years) and enrolment.current_year >= max(years)


def _year_minimum_months(db: Session, enrolment: Enrolment) -> int:
    version = db.get(CurriculumVersion, enrolment.curriculum_version_id)
    if not version:
        return 12
    cumulative = 0
    for year in sorted(version.training_years, key=lambda y: y.sequence):
        cumulative += year.duration_months
        if year.sequence == enrolment.current_year:
            return cumulative
    return cumulative or 12


def _rotation_check(db: Session, enrolment: Enrolment) -> dict[str, Any]:
    """Mandatory rotations for the current year must be closed as completed."""
    current_year_rotations = [
        r for r in enrolment.rotations if r.training_year == enrolment.current_year
    ]
    outstanding = [
        {"id": r.id, "name": r.name, "status": r.status, "end_date": str(r.end_date)}
        for r in current_year_rotations
        if r.status not in {RotationStatus.COMPLETED, RotationStatus.CANCELLED}
    ]
    failed = [
        {"id": r.id, "name": r.name}
        for r in current_year_rotations
        if r.status == RotationStatus.FAILED
    ]
    return {
        "planned": len(current_year_rotations),
        "completed": sum(1 for r in current_year_rotations if r.status == RotationStatus.COMPLETED),
        "outstanding": outstanding,
        "failed": failed,
        "passed": not outstanding and not failed,
    }


# --------------------------------------------------------------------------
def assess(
    db: Session,
    enrolment: Enrolment,
    *,
    as_of: date | None = None,
    include_scores: bool = True,
) -> PromotionAssessment:
    """Run every promotion gate and produce an explainable recommendation."""
    as_of = as_of or date.today()
    programme = enrolment.programme
    target_level = next_level(enrolment, programme.settings if programme else None)

    # 1. Requirement gates -------------------------------------------------
    rules = load_rules(
        db,
        enrolment.curriculum_version_id,
        scopes=[
            RequirementScope.PROMOTION,
            RequirementScope.TRAINING_YEAR,
            RequirementScope.PROGRAMME,
        ],
        training_year=enrolment.current_year,
    )
    ctx = EvaluationContext(
        db=db, enrolment=enrolment, as_of=as_of, training_year=enrolment.current_year
    )
    results: list[RequirementResult] = evaluate_many(ctx, rules)
    blocking = [r for r in results if not r.met and r.severity == RequirementSeverity.MANDATORY]
    advisories = [r for r in results if not r.met and r.severity == RequirementSeverity.RECOMMENDED]

    # 2. Time served -------------------------------------------------------
    served = months_served(enrolment, as_of)
    required_months = _year_minimum_months(db, enrolment)
    time_ok = served >= required_months

    # 3. Rotation closure --------------------------------------------------
    rotation_check = _rotation_check(db, enrolment)

    # 4. Enrolment standing ------------------------------------------------
    standing_ok = enrolment.status == EnrolmentStatus.ACTIVE

    mandatory = [r for r in results if r.severity == RequirementSeverity.MANDATORY]
    readiness = (
        sum(r.progress_percent for r in mandatory) / len(mandatory) if mandatory else 100.0
    )
    # Time and rotation gates are pass/fail and drag readiness down proportionally.
    gate_factors = [
        min(1.0, served / required_months) if required_months else 1.0,
        1.0 if rotation_check["passed"] else 0.6,
        1.0 if standing_ok else 0.0,
    ]
    readiness = readiness * (sum(gate_factors) / len(gate_factors))

    checks = {
        "requirements": {
            "total": len(results),
            "met": sum(1 for r in results if r.met),
            "blocking_unmet": len(blocking),
            "advisory_unmet": len(advisories),
            "passed": not blocking,
        },
        "time_served": {
            "months_served": served,
            "months_required": required_months,
            "interruption_days": enrolment.interruption_days,
            "passed": time_ok,
        },
        "rotations": rotation_check,
        "standing": {"status": enrolment.status, "passed": standing_ok},
    }

    score_report = compute_scores(db, enrolment, as_of=as_of, trigger="promotion_review") if include_scores else None

    # 5. Verdict -----------------------------------------------------------
    reasons: list[str] = []
    if not standing_ok:
        reasons.append(f"Enrolment status is '{enrolment.status}', not active.")
    if not time_ok:
        reasons.append(
            f"{served} of {required_months} required training months served."
        )
    if not rotation_check["passed"]:
        if rotation_check["failed"]:
            reasons.append(f"{len(rotation_check['failed'])} rotation(s) recorded as failed.")
        if rotation_check["outstanding"]:
            reasons.append(
                f"{len(rotation_check['outstanding'])} rotation(s) not yet closed as completed."
            )
    if blocking:
        reasons.append(
            f"{len(blocking)} mandatory requirement(s) unmet: "
            + "; ".join(r.label for r in blocking[:3])
            + ("…" if len(blocking) > 3 else "")
        )

    if not reasons:
        outcome = PromotionOutcome.RECOMMENDED
        rationale = (
            f"All promotion gates cleared: {checks['requirements']['met']} of "
            f"{checks['requirements']['total']} requirements met, {served} months served "
            f"(minimum {required_months}), all year-{enrolment.current_year} rotations closed."
        )
        if advisories:
            rationale += f" {len(advisories)} advisory requirement(s) remain outstanding."
    elif len(reasons) == 1 and not blocking and not time_ok and served >= required_months - 2:
        # Within two months of the minimum with everything else clear — defer rather
        # than decline, so the committee revisits instead of restarting the process.
        outcome = PromotionOutcome.DEFERRED
        rationale = "Deferred pending completion of minimum training time: " + reasons[0]
    else:
        outcome = PromotionOutcome.NOT_RECOMMENDED
        rationale = "Not recommended. " + " ".join(reasons)

    return PromotionAssessment(
        enrolment_id=enrolment.id,
        from_level=enrolment.current_level,
        to_level=target_level,
        from_year=enrolment.current_year,
        to_year=enrolment.current_year + 1,
        outcome=outcome,
        readiness_percent=max(0.0, min(100.0, readiness)),
        blocking=[r.to_dict() for r in blocking],
        advisories=[r.to_dict() for r in advisories],
        rationale=rationale,
        time_served_months=served,
        minimum_months_required=required_months,
        checks=checks,
        score_report=score_report,
    )


def exam_eligibility(
    db: Session, enrolment: Enrolment, *, as_of: date | None = None
) -> dict[str, Any]:
    """Eligibility to sit a college examination — a separate gate from promotion."""
    as_of = as_of or date.today()
    rules = load_rules(
        db,
        enrolment.curriculum_version_id,
        scopes=[RequirementScope.EXAM_ELIGIBILITY],
        training_year=enrolment.current_year,
    )
    ctx = EvaluationContext(
        db=db, enrolment=enrolment, as_of=as_of, training_year=enrolment.current_year
    )
    results = evaluate_many(ctx, rules)
    blocking = [r for r in results if not r.met and r.severity == RequirementSeverity.MANDATORY]
    programme = enrolment.programme
    return {
        "enrolment_id": enrolment.id,
        "awarding_body": programme.awarding_body if programme else None,
        "programme_type": programme.programme_type if programme else None,
        "eligible": not blocking,
        "requirements": [r.to_dict() for r in results],
        "blocking": [r.to_dict() for r in blocking],
        "assessed_on": str(as_of),
    }


def record_review(
    db: Session,
    enrolment: Enrolment,
    assessment: PromotionAssessment,
    *,
    review_date: date | None = None,
) -> PromotionReview:
    """Persist the engine's assessment so the committee has something to ratify."""
    snapshot: ScoreSnapshot | None = None
    if assessment.score_report is not None:
        snapshot = persist_snapshot(
            db, enrolment, assessment.score_report, trigger="promotion_review"
        )
        db.flush()

    review = PromotionReview(
        tenant_id=enrolment.tenant_id,
        enrolment_id=enrolment.id,
        snapshot_id=snapshot.id if snapshot else None,
        review_date=review_date or date.today(),
        from_level=assessment.from_level,
        to_level=assessment.to_level,
        from_year=assessment.from_year,
        to_year=assessment.to_year,
        engine_outcome=assessment.outcome,
        engine_readiness_percent=assessment.readiness_percent,
        blocking_requirements=assessment.blocking,
        engine_rationale=assessment.rationale,
    )
    db.add(review)

    enrolment.promotion_ready = assessment.is_recommended
    db.add(enrolment)
    return review


def apply_decision(
    db: Session,
    review: PromotionReview,
    *,
    outcome: str,
    decided_by_id: str,
    effective_date: date | None = None,
    conditions: list[str] | None = None,
    note: str | None = None,
    override_reason: str | None = None,
) -> PromotionReview:
    """Ratify (or overturn) the recommendation and advance the trainee if approved.

    An outcome that contradicts the engine requires ``override_reason``; the platform
    refuses the write otherwise, so the audit trail can never lose the justification.
    """
    engine_said_yes = review.engine_outcome == PromotionOutcome.RECOMMENDED
    human_says_yes = outcome == PromotionOutcome.APPROVED
    if engine_said_yes != human_says_yes and not override_reason:
        raise ValueError(
            "A decision that differs from the engine recommendation requires an "
            "override reason."
        )

    review.outcome = outcome
    review.decided_by_id = decided_by_id
    review.decided_at = utcnow()
    review.effective_date = effective_date or date.today()
    review.conditions = conditions or []
    review.decision_note = note
    review.override_reason = override_reason
    db.add(review)

    if outcome in {PromotionOutcome.APPROVED, PromotionOutcome.CONDITIONAL}:
        enrolment = db.get(Enrolment, review.enrolment_id)
        if enrolment is not None:
            _advance(db, enrolment, review)
    return review


def _advance(db: Session, enrolment: Enrolment, review: PromotionReview) -> None:
    """Move the trainee to the next year/level, or complete the programme."""
    if _is_final_year(db, enrolment):
        programme = enrolment.programme
        terminal = programme and programme.programme_type in {
            ProgrammeType.HOUSEMANSHIP,
            ProgrammeType.INTERNSHIP,
            ProgrammeType.FELLOWSHIP,
            ProgrammeType.SUBSPECIALTY_FELLOWSHIP,
        }
        enrolment.status = EnrolmentStatus.COMPLETED
        enrolment.actual_end_date = review.effective_date
        if terminal:
            enrolment.current_level = review.to_level
    else:
        enrolment.current_year = review.to_year
        enrolment.current_level = review.to_level
        # Level only changes at defined boundaries; a mid-programme year change keeps
        # the existing level unless the curriculum's training year says otherwise.
        year_row = next(
            (
                y
                for y in (db.get(CurriculumVersion, enrolment.curriculum_version_id).training_years
                          if db.get(CurriculumVersion, enrolment.curriculum_version_id) else [])
                if y.sequence == review.to_year
            ),
            None,
        )
        if isinstance(year_row, TrainingYear):
            enrolment.current_level = year_row.level
    enrolment.promotion_ready = False
    db.add(enrolment)


def batch_assess(
    db: Session, enrolments: list[Enrolment], *, as_of: date | None = None
) -> list[PromotionAssessment]:
    """Cohort-wide readiness — what the HOD and Director dashboards render."""
    return [assess(db, e, as_of=as_of, include_scores=False) for e in enrolments]
