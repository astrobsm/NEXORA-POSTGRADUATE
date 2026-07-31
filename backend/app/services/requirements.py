"""The requirement evaluation engine.

Everything in RTC that says "a trainee must do X before Y" resolves to a
:class:`~app.models.curriculum.RequirementRule` row evaluated here. Adding a college
regulation, tightening a departmental minimum, or introducing an entirely new kind of
training requirement is a *data* operation — the only code that ever changes is this
module, and only when a genuinely new *kind of measurement* is invented.

The engine is deliberately split in two:

``MEASURERS``
    One function per :class:`RequirementKind` that answers *"how much has this trainee
    actually done?"* — returning a measured value plus the raw counts behind it.

``evaluate_rule``
    Applies the rule's operator and target to the measurement and produces a verdict
    that is fully explainable: measured, target, operator, shortfall, and the evidence
    counts. Nothing about a pass/fail decision is opaque to the trainee.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.academic import ActivityParticipant, AcademicActivity, ConferenceRecord
from app.models.assessment import Assessment, AssessmentTemplate, CompetencyRating
from app.models.cbt import ExamAttempt
from app.models.cme import CmeCreditLedger
from app.models.curriculum import Competency, RequirementRule
from app.models.duty import AttendanceRecord, DutyShift
from app.models.enums import (
    DISSERTATION_STAGE_ORDER,
    ENTRUSTMENT_ORDER,
    AttendanceStatus,
    ParticipantRole,
    RequirementKind,
    RequirementOperator,
    RequirementScope,
    RequirementSeverity,
    RotationStatus,
    ValidationStatus,
)
from app.models.logbook import LogEntry, TeachingRecord
from app.models.research import Publication, ResearchProject
from app.models.training import Enrolment, RotationAssignment

# --------------------------------------------------------------------------
# Context & results
# --------------------------------------------------------------------------


@dataclass(slots=True)
class EvaluationContext:
    """Everything a measurer needs, assembled once per evaluation run."""

    db: Session
    enrolment: Enrolment
    as_of: date
    #: Restrict measurement to this training year when the rule is year-scoped.
    training_year: int | None = None
    #: Restrict measurement to this rotation when the rule is rotation-scoped.
    rotation: RotationAssignment | None = None
    #: Memoised measurements within a run — rules frequently share a measurement.
    _cache: dict[tuple[Any, ...], Measurement] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class Measurement:
    value: float
    #: Denominator for percentage measurements (e.g. sessions expected).
    denominator: float | None = None
    #: Raw evidence supporting the number, surfaced in the UI on drill-down.
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RequirementResult:
    rule_id: str
    code: str | None
    label: str
    kind: str
    scope: str
    severity: str
    operator: str
    target: float
    measured: float
    met: bool
    #: How far short (0 when met). Percentage-of-target for progress bars.
    shortfall: float
    progress_percent: float
    weight: float
    score_domain: str | None
    detail: dict[str, Any] = field(default_factory=dict)
    guidance: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "code": self.code,
            "label": self.label,
            "kind": self.kind,
            "scope": self.scope,
            "severity": self.severity,
            "operator": self.operator,
            "target": round(self.target, 2),
            "measured": round(self.measured, 2),
            "met": self.met,
            "shortfall": round(self.shortfall, 2),
            "progress_percent": round(self.progress_percent, 1),
            "weight": self.weight,
            "score_domain": self.score_domain,
            "detail": self.detail,
            "guidance": self.guidance,
        }


# --------------------------------------------------------------------------
# Windowing
# --------------------------------------------------------------------------
def training_year_window(enrolment: Enrolment, year: int) -> tuple[date, date]:
    """The calendar window covered by a given training year of an enrolment.

    Interruptions push the window forward: a trainee who took six months of maternity
    leave in year 2 has year 3 shifted accordingly, so year-scoped requirements are not
    unfairly assessed against a shorter period.
    """
    offset_months = 12 * (year - 1)
    start = enrolment.start_date + relativedelta(months=offset_months)
    start += timedelta(days=enrolment.interruption_days if year > 1 else 0)
    end = start + relativedelta(months=12) - timedelta(days=1)
    return start, end


def resolve_window(ctx: EvaluationContext, rule: RequirementRule) -> tuple[date, date]:
    """Determine the date range over which a rule is measured."""
    params = rule.parameters or {}

    if rule.scope == RequirementScope.ROTATION and ctx.rotation is not None:
        return ctx.rotation.start_date, min(ctx.rotation.end_date, ctx.as_of)

    explicit_year = params.get("training_year") or ctx.training_year
    if rule.scope == RequirementScope.TRAINING_YEAR and explicit_year:
        start, end = training_year_window(ctx.enrolment, int(explicit_year))
        return start, min(end, ctx.as_of)

    # Programme / promotion / exam-eligibility scopes measure the whole enrolment.
    return ctx.enrolment.start_date, ctx.as_of


# --------------------------------------------------------------------------
# Shared query fragments
# --------------------------------------------------------------------------
def _validated_log_query(ctx: EvaluationContext, window: tuple[date, date]):
    start, end = window
    stmt = select(LogEntry).where(
        LogEntry.enrolment_id == ctx.enrolment.id,
        LogEntry.validation_status == ValidationStatus.VALIDATED,
        LogEntry.deleted_at.is_(None),
        LogEntry.occurred_on >= start,
        LogEntry.occurred_on <= end,
    )
    if ctx.rotation is not None:
        stmt = stmt.where(LogEntry.rotation_assignment_id == ctx.rotation.id)
    return stmt


def _apply_log_filters(stmt, params: dict[str, Any]):
    """Translate rule parameters into logbook predicates."""
    if entry_types := params.get("entry_types"):
        stmt = stmt.where(LogEntry.entry_type.in_(entry_types))
    if roles := params.get("roles"):
        stmt = stmt.where(LogEntry.participation_role.in_(roles))
    elif role := params.get("role"):
        stmt = stmt.where(LogEntry.participation_role == role)
    if grade := params.get("grade"):
        stmt = stmt.where(LogEntry.procedure_grade == grade)
    if procedure_ids := params.get("procedure_ids"):
        stmt = stmt.where(LogEntry.procedure_id.in_(procedure_ids))
    if procedure_codes := params.get("procedure_codes"):
        # Match on the denormalised name when the catalogue link is absent (offline entry).
        stmt = stmt.where(LogEntry.procedure_name.in_(procedure_codes))
    if complexities := params.get("complexities"):
        stmt = stmt.where(LogEntry.complexity.in_(complexities))
    if org_unit_ids := params.get("org_unit_ids"):
        stmt = stmt.where(LogEntry.org_unit_id.in_(org_unit_ids))
    return stmt


def _sum_quantity(ctx: EvaluationContext, stmt) -> tuple[float, int]:
    """Return (total quantity, row count) for a logbook query."""
    rows = ctx.db.execute(stmt).scalars().all()
    return float(sum(r.quantity or 1 for r in rows)), len(rows)


# --------------------------------------------------------------------------
# Measurers — one per RequirementKind
# --------------------------------------------------------------------------
Measurer = Callable[[EvaluationContext, RequirementRule, tuple[date, date]], Measurement]


def measure_procedure_count(ctx, rule, window) -> Measurement:
    params = rule.parameters or {}
    stmt = _apply_log_filters(_validated_log_query(ctx, window), params)
    if not params.get("entry_types"):
        stmt = stmt.where(
            LogEntry.entry_type.in_(["major_procedure", "minor_procedure"])
        )
    total, rows = _sum_quantity(ctx, stmt)
    return Measurement(total, detail={"entries": rows, "window": [str(window[0]), str(window[1])]})


def measure_procedure_role_count(ctx, rule, window) -> Measurement:
    """Count procedures at a given participation role.

    When ``weighted`` is set, roles contribute at their competence weight (observing
    counts for less than performing independently) — a closer proxy for capability than
    a flat headcount, and the mode most colleges are moving toward.
    """
    params = rule.parameters or {}
    stmt = _apply_log_filters(_validated_log_query(ctx, window), params)
    rows = ctx.db.execute(stmt).scalars().all()

    if params.get("weighted"):
        from app.models.enums import PARTICIPATION_WEIGHT

        total = sum(
            (r.quantity or 1) * PARTICIPATION_WEIGHT.get(r.participation_role or "", 1.0)
            for r in rows
        )
    else:
        total = float(sum(r.quantity or 1 for r in rows))

    by_role: dict[str, int] = {}
    for r in rows:
        by_role[r.participation_role or "unspecified"] = by_role.get(
            r.participation_role or "unspecified", 0
        ) + (r.quantity or 1)
    return Measurement(total, detail={"by_role": by_role, "entries": len(rows)})


def measure_logbook_entry_count(ctx, rule, window) -> Measurement:
    stmt = _apply_log_filters(_validated_log_query(ctx, window), rule.parameters or {})
    total, rows = _sum_quantity(ctx, stmt)
    return Measurement(total, detail={"entries": rows})


def measure_clinic_count(ctx, rule, window) -> Measurement:
    stmt = _validated_log_query(ctx, window).where(LogEntry.entry_type == "clinic")
    total, rows = _sum_quantity(ctx, stmt)
    return Measurement(total, detail={"entries": rows})


def measure_ward_round_count(ctx, rule, window) -> Measurement:
    stmt = _validated_log_query(ctx, window).where(LogEntry.entry_type == "ward_round")
    total, rows = _sum_quantity(ctx, stmt)
    return Measurement(total, detail={"entries": rows})


def _latest_competency_levels(
    ctx: EvaluationContext, competency_ids: list[str], window: tuple[date, date]
) -> dict[str, int]:
    """Latest rating per competency — progression is judged on the most recent view."""
    if not competency_ids:
        return {}
    stmt = (
        select(CompetencyRating)
        .where(
            CompetencyRating.enrolment_id == ctx.enrolment.id,
            CompetencyRating.competency_id.in_(competency_ids),
            CompetencyRating.rated_on <= window[1],
            CompetencyRating.is_self_rating.is_(False),
        )
        .order_by(CompetencyRating.rated_on.asc())
    )
    latest: dict[str, int] = {}
    for rating in ctx.db.execute(stmt).scalars():
        latest[rating.competency_id] = rating.level_value
    return latest


def _resolve_competency_ids(ctx: EvaluationContext, rule: RequirementRule) -> list[str]:
    params = rule.parameters or {}
    if rule.competency_id:
        return [rule.competency_id]
    stmt = select(Competency.id).where(
        Competency.curriculum_version_id == ctx.enrolment.curriculum_version_id
    )
    if codes := params.get("competency_codes"):
        stmt = stmt.where(Competency.code.in_(codes))
    if domains := params.get("domains"):
        stmt = stmt.where(Competency.domain.in_(domains))
    if params.get("epas_only") or rule.kind == RequirementKind.EPA_LEVEL:
        stmt = stmt.where(Competency.is_epa.is_(True))
    return list(ctx.db.execute(stmt).scalars().all())


def measure_competency_level(ctx, rule, window) -> Measurement:
    """Measure attainment against target entrustment levels.

    ``aggregate`` selects the statistic: ``min`` (the default — every competency must
    reach the target), ``mean``, or ``percent_at_target``.
    """
    params = rule.parameters or {}
    competency_ids = _resolve_competency_ids(ctx, rule)
    levels = _latest_competency_levels(ctx, competency_ids, window)
    aggregate = params.get("aggregate", "min")

    if not competency_ids:
        return Measurement(0.0, detail={"reason": "no matching competencies"})

    values = [levels.get(cid, 0) for cid in competency_ids]
    rated = [v for v in values if v > 0]
    unrated = len(values) - len(rated)

    if aggregate == "mean":
        value = (sum(values) / len(values)) if values else 0.0
    elif aggregate == "percent_at_target":
        target_level = int(params.get("level_value", rule.target_value or 4))
        at_target = sum(1 for v in values if v >= target_level)
        value = (at_target / len(values)) * 100 if values else 0.0
    else:  # min
        value = float(min(values)) if values else 0.0

    return Measurement(
        value,
        denominator=float(len(values)),
        detail={
            "competencies_total": len(values),
            "competencies_rated": len(rated),
            "competencies_unrated": unrated,
            "aggregate": aggregate,
            "distribution": {str(v): values.count(v) for v in sorted(set(values))},
        },
    )


measure_epa_level = measure_competency_level


def measure_academic_attendance_pct(ctx, rule, window) -> Measurement:
    """Attendance as a percentage of the sessions the trainee was expected to attend.

    "Expected" means: mandatory activities held in the trainee's department (or the
    activity kinds named in the rule) during the window, while the trainee was not on
    approved leave.
    """
    params = rule.parameters or {}
    start, end = window
    activity_kinds = params.get("activity_kinds")

    expected_stmt = select(AcademicActivity.id).where(
        AcademicActivity.tenant_id == ctx.enrolment.tenant_id,
        AcademicActivity.scheduled_on >= start,
        AcademicActivity.scheduled_on <= end,
        AcademicActivity.deleted_at.is_(None),
    )
    if activity_kinds:
        expected_stmt = expected_stmt.where(AcademicActivity.kind.in_(activity_kinds))
    if params.get("mandatory_only", True):
        expected_stmt = expected_stmt.where(AcademicActivity.is_mandatory.is_(True))
    if org_unit_ids := params.get("org_unit_ids"):
        expected_stmt = expected_stmt.where(AcademicActivity.org_unit_id.in_(org_unit_ids))
    else:
        expected_stmt = expected_stmt.where(
            AcademicActivity.org_unit_id == ctx.enrolment.org_unit_id
        )

    expected_ids = list(ctx.db.execute(expected_stmt).scalars().all())
    if not expected_ids:
        return Measurement(0.0, denominator=0.0, detail={"expected": 0, "attended": 0,
                                                         "note": "no qualifying sessions in window"})

    attended = ctx.db.execute(
        select(func.count())
        .select_from(ActivityParticipant)
        .where(
            ActivityParticipant.activity_id.in_(expected_ids),
            ActivityParticipant.user_id == ctx.enrolment.trainee_id,
            ActivityParticipant.attended.is_(True),
        )
    ).scalar_one()

    pct = (attended / len(expected_ids)) * 100
    return Measurement(
        pct,
        denominator=float(len(expected_ids)),
        detail={"expected": len(expected_ids), "attended": int(attended),
                "activity_kinds": activity_kinds or "all mandatory"},
    )


def measure_duty_attendance_pct(ctx, rule, window) -> Measurement:
    params = rule.parameters or {}
    start, end = window
    shift_stmt = select(DutyShift.id).where(
        DutyShift.user_id == ctx.enrolment.trainee_id,
        func.date(DutyShift.starts_at) >= start,
        func.date(DutyShift.starts_at) <= end,
    )
    if duty_kinds := params.get("duty_kinds"):
        shift_stmt = shift_stmt.where(DutyShift.duty_kind.in_(duty_kinds))
    shift_ids = list(ctx.db.execute(shift_stmt).scalars().all())
    if not shift_ids:
        return Measurement(0.0, denominator=0.0, detail={"scheduled": 0, "attended": 0})

    present_statuses = [
        AttendanceStatus.PRESENT,
        AttendanceStatus.LATE,
        AttendanceStatus.PARTIAL,
        AttendanceStatus.EXCUSED,
    ]
    attended = ctx.db.execute(
        select(func.count())
        .select_from(AttendanceRecord)
        .where(
            AttendanceRecord.shift_id.in_(shift_ids),
            AttendanceRecord.user_id == ctx.enrolment.trainee_id,
            AttendanceRecord.status.in_(present_statuses),
        )
    ).scalar_one()

    return Measurement(
        (attended / len(shift_ids)) * 100,
        denominator=float(len(shift_ids)),
        detail={"scheduled": len(shift_ids), "attended": int(attended)},
    )


def measure_activity_presentation_count(ctx, rule, window) -> Measurement:
    params = rule.parameters or {}
    start, end = window
    roles = params.get("roles", [ParticipantRole.PRESENTER])
    stmt = (
        select(func.count())
        .select_from(ActivityParticipant)
        .join(AcademicActivity, AcademicActivity.id == ActivityParticipant.activity_id)
        .where(
            ActivityParticipant.user_id == ctx.enrolment.trainee_id,
            ActivityParticipant.role.in_(roles),
            AcademicActivity.scheduled_on >= start,
            AcademicActivity.scheduled_on <= end,
        )
    )
    if kinds := params.get("activity_kinds"):
        stmt = stmt.where(AcademicActivity.kind.in_(kinds))
    count = float(ctx.db.execute(stmt).scalar_one())

    if params.get("include_conferences"):
        conf = ctx.db.execute(
            select(func.count())
            .select_from(ConferenceRecord)
            .where(
                ConferenceRecord.user_id == ctx.enrolment.trainee_id,
                ConferenceRecord.start_date >= start,
                ConferenceRecord.end_date <= end,
                ConferenceRecord.participation.in_(["oral_presentation", "poster"]),
            )
        ).scalar_one()
        count += float(conf)

    return Measurement(count, detail={"roles": list(roles)})


def _assessment_query(ctx: EvaluationContext, rule: RequirementRule, window: tuple[date, date]):
    params = rule.parameters or {}
    start, end = window
    stmt = (
        select(Assessment)
        .join(AssessmentTemplate, AssessmentTemplate.id == Assessment.template_id)
        .where(
            Assessment.enrolment_id == ctx.enrolment.id,
            Assessment.deleted_at.is_(None),
            Assessment.status == "approved",
            Assessment.occurred_on >= start,
            Assessment.occurred_on <= end,
        )
    )
    if kinds := params.get("assessment_kinds"):
        stmt = stmt.where(AssessmentTemplate.kind.in_(kinds))
    if codes := params.get("template_codes"):
        stmt = stmt.where(AssessmentTemplate.code.in_(codes))
    if ctx.rotation is not None and rule.scope == RequirementScope.ROTATION:
        stmt = stmt.where(Assessment.rotation_assignment_id == ctx.rotation.id)
    return stmt


def measure_assessment_pass_count(ctx, rule, window) -> Measurement:
    stmt = _assessment_query(ctx, rule, window)
    rows = ctx.db.execute(stmt).scalars().all()
    passed = [a for a in rows if a.is_pass]
    return Measurement(
        float(len(passed)),
        denominator=float(len(rows)),
        detail={"total": len(rows), "passed": len(passed),
                "distinct_assessors": len({a.assessor_id for a in passed if a.assessor_id})},
    )


def measure_assessment_mean_score(ctx, rule, window) -> Measurement:
    stmt = _assessment_query(ctx, rule, window)
    scores = [a.percent_score for a in ctx.db.execute(stmt).scalars() if a.percent_score is not None]
    if not scores:
        return Measurement(0.0, detail={"assessments": 0})
    return Measurement(
        sum(scores) / len(scores),
        denominator=float(len(scores)),
        detail={"assessments": len(scores), "min": min(scores), "max": max(scores)},
    )


def measure_exam_pass(ctx, rule, window) -> Measurement:
    params = rule.parameters or {}
    start, end = window
    stmt = select(ExamAttempt).where(
        ExamAttempt.user_id == ctx.enrolment.trainee_id,
        ExamAttempt.status == "marked",
        func.date(ExamAttempt.started_at) >= start,
        func.date(ExamAttempt.started_at) <= end,
    )
    if paper_ids := params.get("paper_ids"):
        stmt = stmt.where(ExamAttempt.paper_id.in_(paper_ids))
    attempts = ctx.db.execute(stmt).scalars().all()
    passes = [a for a in attempts if a.is_pass]
    best = max((a.percent_score or 0.0) for a in attempts) if attempts else 0.0
    return Measurement(
        float(len(passes)),
        detail={"attempts": len(attempts), "passes": len(passes), "best_percent": round(best, 1)},
    )


def measure_cme_credits(ctx, rule, window) -> Measurement:
    params = rule.parameters or {}
    start, end = window
    stmt = select(func.coalesce(func.sum(CmeCreditLedger.credits), 0.0)).where(
        CmeCreditLedger.user_id == ctx.enrolment.trainee_id,
        CmeCreditLedger.awarded_on >= start,
        CmeCreditLedger.awarded_on <= end,
        CmeCreditLedger.is_reversed.is_(False),
    )
    if body := params.get("recognised_by"):
        stmt = stmt.where(CmeCreditLedger.recognised_by == body)
    total = float(ctx.db.execute(stmt).scalar_one() or 0.0)
    return Measurement(total, detail={"window": [str(start), str(end)]})


def measure_research_output(ctx, rule, window) -> Measurement:
    """Count research projects that have reached at least a given stage."""
    params = rule.parameters or {}
    min_stage = params.get("min_stage", "data_collection")
    try:
        min_index = DISSERTATION_STAGE_ORDER.index(min_stage)
    except ValueError:
        min_index = 0

    stmt = select(ResearchProject).where(
        ResearchProject.enrolment_id == ctx.enrolment.id,
        ResearchProject.deleted_at.is_(None),
    )
    if types := params.get("research_types"):
        stmt = stmt.where(ResearchProject.research_type.in_(types))
    projects = ctx.db.execute(stmt).scalars().all()
    qualifying = [
        p for p in projects
        if p.current_stage in DISSERTATION_STAGE_ORDER
        and DISSERTATION_STAGE_ORDER.index(p.current_stage) >= min_index
    ]
    return Measurement(
        float(len(qualifying)),
        detail={"projects": len(projects), "qualifying": len(qualifying), "min_stage": min_stage},
    )


def measure_publication_count(ctx, rule, window) -> Measurement:
    params = rule.parameters or {}
    start, end = window
    stmt = select(Publication).where(
        Publication.user_id == ctx.enrolment.trainee_id,
        Publication.deleted_at.is_(None),
        Publication.year >= start.year,
        Publication.year <= end.year,
    )
    if params.get("verified_only", True):
        stmt = stmt.where(Publication.verification_status == "approved")
    if types := params.get("publication_types"):
        stmt = stmt.where(Publication.publication_type.in_(types))
    if params.get("peer_reviewed_only"):
        stmt = stmt.where(Publication.is_peer_reviewed.is_(True))
    if indexes := params.get("indexed_in"):
        rows = [
            p for p in ctx.db.execute(stmt).scalars()
            if set(p.indexed_in or []) & set(indexes)
        ]
    else:
        rows = list(ctx.db.execute(stmt).scalars().all())

    if max_position := params.get("max_author_position"):
        rows = [p for p in rows if p.author_position <= int(max_position)]

    return Measurement(
        float(len(rows)),
        detail={"publications": len(rows),
                "first_author": sum(1 for p in rows if p.author_position == 1)},
    )


def measure_dissertation_stage(ctx, rule, window) -> Measurement:
    """Furthest dissertation stage reached, as a 0-based index into the workflow."""
    stmt = select(ResearchProject).where(
        ResearchProject.enrolment_id == ctx.enrolment.id,
        ResearchProject.research_type.in_(["dissertation", "thesis"]),
        ResearchProject.deleted_at.is_(None),
    )
    projects = list(ctx.db.execute(stmt).scalars().all())
    if not projects:
        return Measurement(-1.0, detail={"stage": None, "note": "no dissertation registered"})
    indices = [
        DISSERTATION_STAGE_ORDER.index(p.current_stage)
        for p in projects
        if p.current_stage in DISSERTATION_STAGE_ORDER
    ]
    best = max(indices) if indices else -1
    return Measurement(
        float(best),
        detail={"stage": DISSERTATION_STAGE_ORDER[best] if best >= 0 else None,
                "stage_index": best,
                "total_stages": len(DISSERTATION_STAGE_ORDER)},
    )


def measure_rotation_completion(ctx, rule, window) -> Measurement:
    """Percentage of planned rotations that closed as completed.

    When a training year is in scope the rotations are selected by their declared
    ``training_year`` rather than by date. A schedule laid out in weeks never aligns
    exactly with month-based year boundaries, and a rotation that starts three days
    before the nominal year boundary still belongs to that year.
    """
    params = rule.parameters or {}
    start, end = window
    stmt = select(RotationAssignment).where(
        RotationAssignment.enrolment_id == ctx.enrolment.id,
        RotationAssignment.status != RotationStatus.CANCELLED,
    )
    year = params.get("training_year") or ctx.training_year
    if year:
        stmt = stmt.where(RotationAssignment.training_year == int(year))
    else:
        stmt = stmt.where(
            RotationAssignment.start_date >= start, RotationAssignment.start_date <= end
        )
    rotations = list(ctx.db.execute(stmt).scalars().all())
    if not rotations:
        return Measurement(0.0, denominator=0.0, detail={"planned": 0, "completed": 0})
    completed = [r for r in rotations if r.status == RotationStatus.COMPLETED]
    return Measurement(
        (len(completed) / len(rotations)) * 100,
        denominator=float(len(rotations)),
        detail={"planned": len(rotations), "completed": len(completed),
                "failed": sum(1 for r in rotations if r.status == RotationStatus.FAILED)},
    )


def measure_teaching_hours(ctx, rule, window) -> Measurement:
    start, end = window
    stmt = select(TeachingRecord).where(
        TeachingRecord.enrolment_id == ctx.enrolment.id,
        TeachingRecord.occurred_on >= start,
        TeachingRecord.occurred_on <= end,
        TeachingRecord.validation_status == ValidationStatus.VALIDATED,
    )
    records = list(ctx.db.execute(stmt).scalars().all())
    hours = sum(r.duration_minutes for r in records) / 60.0
    return Measurement(hours, detail={"sessions": len(records)})


def measure_custom_expression(ctx, rule, window) -> Measurement:
    """Evaluate a restricted arithmetic expression over other measurements.

    ``parameters`` supplies ``inputs`` (a mapping of names to
    ``{"kind": ..., "parameters": {...}}``) and ``expression`` — a plain arithmetic
    formula over those names. Only arithmetic operators are permitted; no attribute
    access, calls, comprehensions, or names beyond the declared inputs. This keeps
    institution-authored formulas expressive without becoming remote code execution.
    """
    import ast
    import operator as op

    params = rule.parameters or {}
    expression = params.get("expression", "0")
    inputs: dict[str, float] = {}

    for name, spec in (params.get("inputs") or {}).items():
        sub_rule = RequirementRule(
            id=rule.id,
            tenant_id=rule.tenant_id,
            curriculum_version_id=rule.curriculum_version_id,
            label=f"{rule.label}:{name}",
            kind=spec.get("kind", RequirementKind.LOGBOOK_ENTRY_COUNT),
            operator=RequirementOperator.GTE,
            target_value=0.0,
            parameters=spec.get("parameters", {}),
            scope=rule.scope,
        )
        inputs[name] = measure(ctx, sub_rule, window).value

    allowed_binary = {
        ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
        ast.Div: op.truediv, ast.Mod: op.mod, ast.Pow: op.pow,
    }

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in inputs:
                raise ValueError(f"Unknown input '{node.id}' in requirement expression.")
            return float(inputs[node.id])
        if isinstance(node, ast.BinOp) and type(node.op) in allowed_binary:
            right = _eval(node.right)
            if isinstance(node.op, (ast.Div, ast.Mod)) and right == 0:
                return 0.0
            return allowed_binary[type(node.op)](_eval(node.left), right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = _eval(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        raise ValueError("Unsupported syntax in requirement expression.")

    try:
        value = _eval(ast.parse(expression, mode="eval"))
    except (ValueError, SyntaxError, TypeError) as exc:
        return Measurement(0.0, detail={"error": str(exc), "expression": expression})
    return Measurement(value, detail={"expression": expression, "inputs": inputs})


MEASURERS: dict[str, Measurer] = {
    RequirementKind.PROCEDURE_COUNT: measure_procedure_count,
    RequirementKind.PROCEDURE_ROLE_COUNT: measure_procedure_role_count,
    RequirementKind.LOGBOOK_ENTRY_COUNT: measure_logbook_entry_count,
    RequirementKind.CLINIC_COUNT: measure_clinic_count,
    RequirementKind.WARD_ROUND_COUNT: measure_ward_round_count,
    RequirementKind.COMPETENCY_LEVEL: measure_competency_level,
    RequirementKind.EPA_LEVEL: measure_epa_level,
    RequirementKind.ACADEMIC_ATTENDANCE_PCT: measure_academic_attendance_pct,
    RequirementKind.DUTY_ATTENDANCE_PCT: measure_duty_attendance_pct,
    RequirementKind.ACTIVITY_PRESENTATION_COUNT: measure_activity_presentation_count,
    RequirementKind.ASSESSMENT_PASS_COUNT: measure_assessment_pass_count,
    RequirementKind.ASSESSMENT_MEAN_SCORE: measure_assessment_mean_score,
    RequirementKind.EXAM_PASS: measure_exam_pass,
    RequirementKind.CME_CREDITS: measure_cme_credits,
    RequirementKind.RESEARCH_OUTPUT: measure_research_output,
    RequirementKind.PUBLICATION_COUNT: measure_publication_count,
    RequirementKind.DISSERTATION_STAGE: measure_dissertation_stage,
    RequirementKind.ROTATION_COMPLETION: measure_rotation_completion,
    RequirementKind.TEACHING_HOURS: measure_teaching_hours,
    RequirementKind.CUSTOM_EXPRESSION: measure_custom_expression,
}


def measure(ctx: EvaluationContext, rule: RequirementRule, window: tuple[date, date]) -> Measurement:
    measurer = MEASURERS.get(rule.kind)
    if measurer is None:
        return Measurement(0.0, detail={"error": f"No measurer registered for kind '{rule.kind}'."})

    cache_key = (rule.kind, repr(sorted((rule.parameters or {}).items())), window, rule.competency_id)
    if cache_key in ctx._cache:
        return ctx._cache[cache_key]
    result = measurer(ctx, rule, window)
    ctx._cache[cache_key] = result
    return result


# --------------------------------------------------------------------------
# Comparison & evaluation
# --------------------------------------------------------------------------
_COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    RequirementOperator.GTE: lambda measured, target: measured >= target,
    RequirementOperator.GT: lambda measured, target: measured > target,
    RequirementOperator.LTE: lambda measured, target: measured <= target,
    RequirementOperator.LT: lambda measured, target: measured < target,
    RequirementOperator.EQ: lambda measured, target: abs(measured - target) < 1e-9,
    RequirementOperator.NEQ: lambda measured, target: abs(measured - target) >= 1e-9,
}


def _normalise_target(rule: RequirementRule) -> float:
    """Entrustment-level rules may express their target as a level name."""
    params = rule.parameters or {}
    if rule.kind in {RequirementKind.COMPETENCY_LEVEL, RequirementKind.EPA_LEVEL}:
        if params.get("aggregate") == "percent_at_target":
            return float(rule.target_value)
        if level := params.get("level"):
            return float(ENTRUSTMENT_ORDER.get(level, rule.target_value))
    if rule.kind == RequirementKind.DISSERTATION_STAGE:
        if stage := params.get("stage"):
            try:
                return float(DISSERTATION_STAGE_ORDER.index(stage))
            except ValueError:
                return float(rule.target_value)
    return float(rule.target_value)


def evaluate_rule(ctx: EvaluationContext, rule: RequirementRule) -> RequirementResult:
    window = resolve_window(ctx, rule)
    measurement = measure(ctx, rule, window)
    target = _normalise_target(rule)
    comparator = _COMPARATORS.get(rule.operator, _COMPARATORS[RequirementOperator.GTE])
    met = comparator(measurement.value, target)

    if rule.operator in {RequirementOperator.GTE, RequirementOperator.GT}:
        shortfall = max(0.0, target - measurement.value)
        progress = 100.0 if met else (measurement.value / target * 100 if target else 100.0)
    elif rule.operator in {RequirementOperator.LTE, RequirementOperator.LT}:
        shortfall = max(0.0, measurement.value - target)
        progress = 100.0 if met else max(0.0, 100.0 - (shortfall / target * 100 if target else 0.0))
    else:
        shortfall = 0.0 if met else abs(target - measurement.value)
        progress = 100.0 if met else 0.0

    detail = dict(measurement.detail)
    detail["window"] = [str(window[0]), str(window[1])]
    if measurement.denominator is not None:
        detail["denominator"] = measurement.denominator

    return RequirementResult(
        rule_id=rule.id,
        code=rule.code,
        label=rule.label,
        kind=rule.kind,
        scope=rule.scope,
        severity=rule.severity,
        operator=rule.operator,
        target=target,
        measured=measurement.value,
        met=met,
        shortfall=shortfall,
        progress_percent=min(100.0, max(0.0, progress)),
        weight=rule.weight,
        score_domain=rule.score_domain,
        detail=detail,
        guidance=rule.guidance,
    )


def load_rules(
    db: Session,
    curriculum_version_id: str,
    *,
    scopes: list[str] | None = None,
    training_year: int | None = None,
    rotation_template_id: str | None = None,
) -> list[RequirementRule]:
    """Fetch the active rules that apply to a scope."""
    stmt = select(RequirementRule).where(
        RequirementRule.curriculum_version_id == curriculum_version_id,
        RequirementRule.is_active.is_(True),
    )
    if scopes:
        stmt = stmt.where(RequirementRule.scope.in_(scopes))
    if rotation_template_id is not None:
        stmt = stmt.where(RequirementRule.rotation_template_id == rotation_template_id)
    rules = list(db.execute(stmt).scalars().all())

    if training_year is not None:
        # Year-scoped rules apply either by explicit training_year_id linkage or by a
        # ``training_year`` parameter; rules with neither apply to every year.
        def _applies(rule: RequirementRule) -> bool:
            declared = (rule.parameters or {}).get("training_year")
            if declared is None:
                return True
            if isinstance(declared, list):
                return training_year in declared
            return int(declared) == training_year

        rules = [r for r in rules if _applies(r)]
    return rules


def evaluate_many(
    ctx: EvaluationContext, rules: list[RequirementRule]
) -> list[RequirementResult]:
    return [evaluate_rule(ctx, rule) for rule in rules]


def blocking_failures(results: list[RequirementResult]) -> list[RequirementResult]:
    return [r for r in results if not r.met and r.severity == RequirementSeverity.MANDATORY]
