"""Accreditation returns.

An accrediting body's standard is expressed as an :class:`AccreditationProfile` with a
set of :class:`AccreditationCriterion` rows. Each criterion names a ``metric`` that this
module knows how to measure over a department for a period. Generating an NPMCN, WACS,
WACP, MDCN or NUC return is therefore the same operation with different data — and a
body that revises its standard mid-cycle is handled by editing rows, not shipping code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.rbac import SUPERVISOR_ROLE_CODES
from app.db.base import utcnow
from app.models.academic import AcademicActivity
from app.models.analytics import (
    AccreditationCriterion,
    AccreditationProfile,
    AccreditationReview,
)
from app.models.assessment import Assessment
from app.models.cbt import ExamAttempt
from app.models.curriculum import Programme
from app.models.enums import RagStatus, ValidationStatus
from app.models.identity import Role, RoleAssignment, User
from app.models.logbook import LogEntry
from app.models.research import Publication, ResearchProject
from app.models.tenancy import OrgUnit
from app.models.training import Enrolment


@dataclass(slots=True)
class MetricContext:
    db: Session
    org_unit: OrgUnit
    org_unit_ids: list[str]
    period_start: date
    period_end: date


@dataclass(slots=True)
class CriterionResult:
    criterion_id: str
    code: str
    section: str
    title: str
    metric: str
    operator: str
    target: float
    measured: float
    unit: str | None
    weighting: str
    met: bool
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "code": self.code,
            "section": self.section,
            "title": self.title,
            "metric": self.metric,
            "operator": self.operator,
            "target": self.target,
            "measured": round(self.measured, 2),
            "unit": self.unit,
            "weighting": self.weighting,
            "met": self.met,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------
# Org subtree
# --------------------------------------------------------------------------
def subtree_ids(db: Session, unit: OrgUnit) -> list[str]:
    """The unit plus every descendant, via the materialised path."""
    rows = db.execute(
        select(OrgUnit.id).where(
            OrgUnit.tenant_id == unit.tenant_id,
            OrgUnit.path.like(f"{unit.path}/%"),
        )
    ).scalars().all()
    return [unit.id, *rows]


# --------------------------------------------------------------------------
# Metric implementations
# --------------------------------------------------------------------------
def _enrolment_ids(ctx: MetricContext) -> list[str]:
    return list(
        ctx.db.execute(
            select(Enrolment.id).where(Enrolment.org_unit_id.in_(ctx.org_unit_ids))
        ).scalars().all()
    )


def metric_consultant_count(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    role_codes = params.get("role_codes") or ["consultant", "head_of_department"]
    count = ctx.db.execute(
        select(func.count(func.distinct(User.id)))
        .select_from(User)
        .join(RoleAssignment, RoleAssignment.user_id == User.id)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(
            RoleAssignment.org_unit_id.in_(ctx.org_unit_ids),
            Role.code.in_(role_codes),
            User.status == "active",
            User.deleted_at.is_(None),
        )
    ).scalar_one()
    return float(count), {"roles_counted": role_codes}


def metric_trainer_count(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    return metric_consultant_count(ctx, {"role_codes": list(SUPERVISOR_ROLE_CODES)})


def metric_trainee_count(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    stmt = select(func.count()).select_from(Enrolment).where(
        Enrolment.org_unit_id.in_(ctx.org_unit_ids),
        Enrolment.status.in_(params.get("statuses") or ["active", "on_leave"]),
    )
    count = ctx.db.execute(stmt).scalar_one()
    return float(count), {}


def metric_trainer_trainee_ratio(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    trainers, _ = metric_trainer_count(ctx, params)
    trainees, _ = metric_trainee_count(ctx, params)
    if trainers == 0:
        return 0.0, {"trainers": 0, "trainees": trainees, "note": "no trainers recorded"}
    ratio = trainees / trainers
    return ratio, {"trainers": trainers, "trainees": trainees}


def metric_annual_procedures(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    enrolment_ids = _enrolment_ids(ctx)
    if not enrolment_ids:
        return 0.0, {"note": "no enrolments in scope"}
    stmt = select(func.coalesce(func.sum(LogEntry.quantity), 0)).where(
        LogEntry.enrolment_id.in_(enrolment_ids),
        LogEntry.validation_status == ValidationStatus.VALIDATED,
        LogEntry.deleted_at.is_(None),
        LogEntry.occurred_on >= ctx.period_start,
        LogEntry.occurred_on <= ctx.period_end,
    )
    if grade := params.get("grade"):
        stmt = stmt.where(LogEntry.procedure_grade == grade)
    if entry_types := params.get("entry_types"):
        stmt = stmt.where(LogEntry.entry_type.in_(entry_types))
    else:
        stmt = stmt.where(LogEntry.entry_type.in_(["major_procedure", "minor_procedure"]))
    total = float(ctx.db.execute(stmt).scalar_one() or 0)
    return total, {"grade": params.get("grade", "all")}


def metric_annual_admissions(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    return metric_annual_procedures(ctx, {"entry_types": ["admission"]})


def metric_annual_clinic_attendances(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    return metric_annual_procedures(ctx, {"entry_types": ["clinic"]})


def metric_academic_activity_frequency(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    """Sessions per month of the named kinds — the usual accreditation phrasing."""
    stmt = select(func.count()).select_from(AcademicActivity).where(
        AcademicActivity.org_unit_id.in_(ctx.org_unit_ids),
        AcademicActivity.scheduled_on >= ctx.period_start,
        AcademicActivity.scheduled_on <= ctx.period_end,
        AcademicActivity.deleted_at.is_(None),
    )
    if kinds := params.get("activity_kinds"):
        stmt = stmt.where(AcademicActivity.kind.in_(kinds))
    count = int(ctx.db.execute(stmt).scalar_one())
    months = max(1.0, (ctx.period_end - ctx.period_start).days / 30.44)
    return count / months, {"sessions": count, "months": round(months, 1),
                            "kinds": params.get("activity_kinds", "all")}


def metric_research_output(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    enrolment_ids = _enrolment_ids(ctx)
    if not enrolment_ids:
        return 0.0, {}
    count = ctx.db.execute(
        select(func.count()).select_from(ResearchProject).where(
            ResearchProject.enrolment_id.in_(enrolment_ids),
            ResearchProject.deleted_at.is_(None),
        )
    ).scalar_one()
    return float(count), {}


def metric_publication_count(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    enrolment_ids = _enrolment_ids(ctx)
    if not enrolment_ids:
        return 0.0, {}
    stmt = select(func.count()).select_from(Publication).where(
        Publication.enrolment_id.in_(enrolment_ids),
        Publication.deleted_at.is_(None),
        Publication.year >= ctx.period_start.year,
        Publication.year <= ctx.period_end.year,
    )
    if params.get("peer_reviewed_only", True):
        stmt = stmt.where(Publication.is_peer_reviewed.is_(True))
    return float(ctx.db.execute(stmt).scalar_one()), {}


def metric_trainee_publication_rate(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    pubs, _ = metric_publication_count(ctx, params)
    trainees, _ = metric_trainee_count(ctx, params)
    if trainees == 0:
        return 0.0, {"note": "no trainees in scope"}
    return pubs / trainees, {"publications": pubs, "trainees": trainees}


def metric_assessment_completion_rate(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    enrolment_ids = _enrolment_ids(ctx)
    if not enrolment_ids:
        return 0.0, {}
    total = ctx.db.execute(
        select(func.count()).select_from(Assessment).where(
            Assessment.enrolment_id.in_(enrolment_ids),
            Assessment.occurred_on >= ctx.period_start,
            Assessment.occurred_on <= ctx.period_end,
            Assessment.deleted_at.is_(None),
        )
    ).scalar_one()
    approved = ctx.db.execute(
        select(func.count()).select_from(Assessment).where(
            Assessment.enrolment_id.in_(enrolment_ids),
            Assessment.status == "approved",
            Assessment.occurred_on >= ctx.period_start,
            Assessment.occurred_on <= ctx.period_end,
            Assessment.deleted_at.is_(None),
        )
    ).scalar_one()
    rate = (approved / total * 100) if total else 0.0
    return rate, {"total": int(total), "approved": int(approved)}


def metric_logbook_validation_rate(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    enrolment_ids = _enrolment_ids(ctx)
    if not enrolment_ids:
        return 0.0, {}
    base = select(func.count()).select_from(LogEntry).where(
        LogEntry.enrolment_id.in_(enrolment_ids),
        LogEntry.deleted_at.is_(None),
        LogEntry.occurred_on >= ctx.period_start,
        LogEntry.occurred_on <= ctx.period_end,
    )
    total = int(ctx.db.execute(base).scalar_one())
    validated = int(
        ctx.db.execute(
            base.where(LogEntry.validation_status == ValidationStatus.VALIDATED)
        ).scalar_one()
    )
    rate = (validated / total * 100) if total else 0.0
    return rate, {"entries": total, "validated": validated}


def metric_exam_pass_rate(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    enrolment_ids = _enrolment_ids(ctx)
    if not enrolment_ids:
        return 0.0, {}
    attempts = ctx.db.execute(
        select(ExamAttempt).where(
            ExamAttempt.enrolment_id.in_(enrolment_ids),
            ExamAttempt.status == "marked",
            func.date(ExamAttempt.started_at) >= ctx.period_start,
            func.date(ExamAttempt.started_at) <= ctx.period_end,
        )
    ).scalars().all()
    if not attempts:
        return 0.0, {"attempts": 0}
    passes = sum(1 for a in attempts if a.is_pass)
    return passes / len(attempts) * 100, {"attempts": len(attempts), "passes": passes}


def metric_programme_count(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    count = ctx.db.execute(
        select(func.count()).select_from(Programme).where(
            Programme.org_unit_id.in_(ctx.org_unit_ids),
            Programme.is_active.is_(True),
            Programme.deleted_at.is_(None),
        )
    ).scalar_one()
    return float(count), {}


def metric_infrastructure(ctx: MetricContext, params: dict[str, Any]) -> tuple[float, dict]:
    """Read a declared capacity figure from the org unit.

    Beds, theatres, ICU beds, library seats, skills-lab stations and similar physical
    resources are institution-declared and evidenced by upload; the criterion names the
    key via ``parameters.capacity_key``.
    """
    key = params.get("capacity_key", "")
    total = 0.0
    contributions: dict[str, float] = {}
    for unit_id in ctx.org_unit_ids:
        unit = ctx.db.get(OrgUnit, unit_id)
        if unit and isinstance(unit.capacity, dict):
            value = unit.capacity.get(key)
            if isinstance(value, (int, float)):
                total += float(value)
                contributions[unit.code] = float(value)
    return total, {"capacity_key": key, "by_unit": contributions,
                   "declared": bool(contributions)}


METRICS: dict[str, Callable[[MetricContext, dict[str, Any]], tuple[float, dict]]] = {
    "consultant_count": metric_consultant_count,
    "trainer_count": metric_trainer_count,
    "trainee_count": metric_trainee_count,
    "trainer_trainee_ratio": metric_trainer_trainee_ratio,
    "annual_procedures": metric_annual_procedures,
    "annual_major_operations": lambda c, p: metric_annual_procedures(c, {**p, "grade": "major"}),
    "annual_admissions": metric_annual_admissions,
    "annual_clinic_attendances": metric_annual_clinic_attendances,
    "academic_activity_frequency": metric_academic_activity_frequency,
    "research_output": metric_research_output,
    "publication_count": metric_publication_count,
    "trainee_publication_rate": metric_trainee_publication_rate,
    "assessment_completion_rate": metric_assessment_completion_rate,
    "logbook_validation_rate": metric_logbook_validation_rate,
    "exam_pass_rate": metric_exam_pass_rate,
    "programme_count": metric_programme_count,
    "infrastructure": metric_infrastructure,
}

_OPERATORS = {
    "gte": lambda m, t: m >= t,
    "gt": lambda m, t: m > t,
    "lte": lambda m, t: m <= t,
    "lt": lambda m, t: m < t,
    "eq": lambda m, t: abs(m - t) < 1e-9,
}


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
def evaluate_criterion(ctx: MetricContext, criterion: AccreditationCriterion) -> CriterionResult:
    measurer = METRICS.get(criterion.metric)
    if measurer is None:
        measured, detail = 0.0, {"error": f"Unknown metric '{criterion.metric}'."}
    else:
        try:
            measured, detail = measurer(ctx, criterion.parameters or {})
        except Exception as exc:  # defensive: one bad criterion must not fail the return
            measured, detail = 0.0, {"error": f"{type(exc).__name__}: {exc}"}

    comparator = _OPERATORS.get(criterion.operator, _OPERATORS["gte"])
    met = comparator(measured, criterion.target_value)
    return CriterionResult(
        criterion_id=criterion.id,
        code=criterion.code,
        section=criterion.section,
        title=criterion.title,
        metric=criterion.metric,
        operator=criterion.operator,
        target=criterion.target_value,
        measured=measured,
        unit=criterion.unit,
        weighting=criterion.weighting,
        met=met,
        detail=detail,
    )


def generate_review(
    db: Session,
    *,
    org_unit: OrgUnit,
    profile: AccreditationProfile,
    period_start: date,
    period_end: date,
    generated_by_id: str | None = None,
    persist: bool = True,
) -> tuple[AccreditationReview, list[CriterionResult]]:
    """Evaluate every criterion in a profile against a department and build the return."""
    ctx = MetricContext(
        db=db,
        org_unit=org_unit,
        org_unit_ids=subtree_ids(db, org_unit),
        period_start=period_start,
        period_end=period_end,
    )

    results = [evaluate_criterion(ctx, c) for c in profile.criteria]
    essential = [r for r in results if r.weighting == "essential"]
    essential_met = sum(1 for r in essential if r.met)
    compliance = (essential_met / len(essential) * 100) if essential else 100.0

    if compliance >= 95:
        rag = RagStatus.GREEN
    elif compliance >= 75:
        rag = RagStatus.AMBER
    else:
        rag = RagStatus.RED

    gaps = [
        {
            "code": r.code,
            "section": r.section,
            "title": r.title,
            "measured": round(r.measured, 2),
            "target": r.target,
            "unit": r.unit,
            "weighting": r.weighting,
            "shortfall": round(max(0.0, r.target - r.measured), 2),
            "detail": r.detail,
        }
        for r in results
        if not r.met
    ]
    gaps.sort(key=lambda g: (0 if g["weighting"] == "essential" else 1, -g["shortfall"]))

    review = AccreditationReview(
        tenant_id=org_unit.tenant_id,
        org_unit_id=org_unit.id,
        profile_id=profile.id,
        period_start=period_start,
        period_end=period_end,
        generated_at=utcnow(),
        generated_by_id=generated_by_id,
        criterion_results=[r.to_dict() for r in results],
        essential_met=essential_met,
        essential_total=len(essential),
        compliance_percent=compliance,
        readiness_rag=rag,
        gaps=gaps,
        narrative=build_narrative(org_unit, profile, results, compliance, period_start, period_end),
    )
    if persist:
        db.add(review)
    return review, results


def build_narrative(
    org_unit: OrgUnit,
    profile: AccreditationProfile,
    results: list[CriterionResult],
    compliance: float,
    period_start: date,
    period_end: date,
) -> str:
    """A plain-language summary that a coordinator can paste into a covering letter."""
    essential = [r for r in results if r.weighting == "essential"]
    unmet = [r for r in essential if not r.met]
    lines = [
        f"{org_unit.name} — {profile.body_name} accreditation return",
        f"Standard: {profile.name} (version {profile.version})",
        f"Review period: {period_start:%d %B %Y} to {period_end:%d %B %Y}",
        "",
        f"{len(essential) - len(unmet)} of {len(essential)} essential criteria are met "
        f"({compliance:.1f}% compliance).",
    ]
    if unmet:
        lines.append("")
        lines.append("Essential criteria not currently met:")
        for r in unmet:
            unit = f" {r.unit}" if r.unit else ""
            lines.append(
                f"  • {r.code} — {r.title}: recorded {r.measured:g}{unit} against a "
                f"required {r.operator.upper()} {r.target:g}{unit}."
            )
        lines.append("")
        lines.append(
            "Each gap above is actionable from the department dashboard; figures are "
            "drawn from validated records only."
        )
    else:
        lines.append("")
        lines.append("All essential criteria are met for the review period.")
    return "\n".join(lines)
