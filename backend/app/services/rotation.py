"""The rotation engine.

Given an enrolment and its curriculum, generate the posting schedule automatically:
sequence the rotations, respect unit capacity, allocate a supervisor to each, and shift
everything downstream when leave, extension or remedial posting intervenes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.curriculum import CurriculumVersion, RotationTemplate
from app.models.enums import (
    RequirementScope,
    RequirementSeverity,
    RotationStatus,
)
from app.models.training import Enrolment, LeaveRecord, RotationAssignment
from app.services.allocation import rank_clinical_supervisors
from app.services.requirements import EvaluationContext, evaluate_many, load_rules


@dataclass(slots=True)
class PlannedRotation:
    template_id: str | None
    name: str
    org_unit_id: str
    training_year: int
    sequence: int
    start_date: date
    end_date: date
    is_elective: bool
    objectives: list[str]
    supervisor_id: str | None = None
    supervisor_rationale: dict[str, Any] | None = None


class RotationPlanningError(Exception):
    """Raised when a schedule cannot be produced from the curriculum as configured."""


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
def plan_schedule(
    db: Session,
    enrolment: Enrolment,
    *,
    from_year: int = 1,
    to_year: int | None = None,
    assign_supervisors: bool = True,
) -> list[PlannedRotation]:
    """Lay out every rotation for the requested training years.

    Rotations run back-to-back from the enrolment start date, each for the duration its
    template declares. Capacity is respected: if a unit is already at
    ``max_trainees`` for the window, the planner reports it rather than silently
    over-filling the posting.
    """
    version = db.get(CurriculumVersion, enrolment.curriculum_version_id)
    if version is None:
        raise RotationPlanningError("Enrolment is not linked to a curriculum version.")

    years = sorted(version.training_years, key=lambda y: y.sequence)
    if not years:
        raise RotationPlanningError(
            f"Curriculum '{version.title}' defines no training years; add at least one "
            "before generating a rotation schedule."
        )

    to_year = to_year or max(y.sequence for y in years)
    planned: list[PlannedRotation] = []
    cursor = enrolment.start_date

    for year in years:
        if year.sequence < from_year:
            cursor = cursor + timedelta(days=round(year.duration_months * 30.44))
            continue
        if year.sequence > to_year:
            break

        templates = sorted(year.rotations, key=lambda r: r.sequence)
        if not templates:
            # A year with no rotation templates is legitimate (e.g. a research year);
            # advance the cursor and continue.
            cursor = cursor + timedelta(days=round(year.duration_months * 30.44))
            continue

        for template in templates:
            start = cursor
            end = start + timedelta(weeks=template.duration_weeks) - timedelta(days=1)
            org_unit_id = template.org_unit_id or enrolment.org_unit_id

            supervisor_id = None
            rationale = None
            if assign_supervisors:
                ranked = rank_clinical_supervisors(
                    db,
                    tenant_id=enrolment.tenant_id,
                    org_unit_id=org_unit_id,
                    trainee_id=enrolment.trainee_id,
                    on=start,
                )
                if ranked:
                    supervisor_id = ranked[0].user_id
                    rationale = ranked[0].as_dict()

            planned.append(
                PlannedRotation(
                    template_id=template.id,
                    name=template.name,
                    org_unit_id=org_unit_id,
                    training_year=year.sequence,
                    sequence=template.sequence,
                    start_date=start,
                    end_date=end,
                    is_elective=template.is_elective,
                    objectives=list(template.objectives or []),
                    supervisor_id=supervisor_id,
                    supervisor_rationale=rationale,
                )
            )
            cursor = end + timedelta(days=1)

    return planned


def capacity_report(
    db: Session, planned: list[PlannedRotation], *, tenant_id: str
) -> list[dict[str, Any]]:
    """Flag postings where the planned schedule would exceed a unit's stated capacity."""
    warnings: list[dict[str, Any]] = []
    for item in planned:
        if item.template_id is None:
            continue
        template = db.get(RotationTemplate, item.template_id)
        if template is None or not template.max_trainees:
            continue
        occupied = db.execute(
            select(func.count())
            .select_from(RotationAssignment)
            .where(
                RotationAssignment.tenant_id == tenant_id,
                RotationAssignment.org_unit_id == item.org_unit_id,
                RotationAssignment.status.in_(
                    [RotationStatus.PLANNED, RotationStatus.ACTIVE, RotationStatus.EXTENDED]
                ),
                RotationAssignment.start_date <= item.end_date,
                RotationAssignment.end_date >= item.start_date,
            )
        ).scalar_one()
        if occupied >= template.max_trainees:
            warnings.append(
                {
                    "rotation": item.name,
                    "org_unit_id": item.org_unit_id,
                    "window": [str(item.start_date), str(item.end_date)],
                    "capacity": template.max_trainees,
                    "already_assigned": int(occupied),
                    "message": (
                        f"{item.name} is at capacity ({occupied}/{template.max_trainees}) "
                        f"for {item.start_date}–{item.end_date}."
                    ),
                }
            )
    return warnings


def materialise(
    db: Session, enrolment: Enrolment, planned: list[PlannedRotation], *, replace: bool = False
) -> list[RotationAssignment]:
    """Persist a planned schedule as rotation assignments.

    ``replace`` removes existing *planned* rotations only; anything already active,
    completed or under remediation is never destroyed by re-planning.
    """
    if replace:
        for existing in list(enrolment.rotations):
            if existing.status == RotationStatus.PLANNED:
                db.delete(existing)
        db.flush()

    created: list[RotationAssignment] = []
    today = date.today()
    for item in planned:
        status = RotationStatus.PLANNED
        if item.start_date <= today <= item.end_date:
            status = RotationStatus.ACTIVE
        elif item.end_date < today:
            status = RotationStatus.PLANNED  # historical gaps are closed explicitly

        assignment = RotationAssignment(
            tenant_id=enrolment.tenant_id,
            enrolment_id=enrolment.id,
            rotation_template_id=item.template_id,
            org_unit_id=item.org_unit_id,
            supervisor_id=item.supervisor_id,
            name=item.name,
            training_year=item.training_year,
            sequence=item.sequence,
            start_date=item.start_date,
            end_date=item.end_date,
            original_end_date=item.end_date,
            status=status,
            is_elective=item.is_elective,
            objectives=item.objectives,
        )
        db.add(assignment)
        created.append(assignment)
    return created


# --------------------------------------------------------------------------
# Lifecycle operations
# --------------------------------------------------------------------------
def evaluate_completion(
    db: Session, rotation: RotationAssignment, *, as_of: date | None = None
) -> dict[str, Any]:
    """Check a rotation's own requirements before it can be signed off."""
    as_of = as_of or date.today()
    enrolment = rotation.enrolment
    rules = load_rules(
        db,
        enrolment.curriculum_version_id,
        scopes=[RequirementScope.ROTATION],
        rotation_template_id=rotation.rotation_template_id,
    )
    ctx = EvaluationContext(
        db=db,
        enrolment=enrolment,
        as_of=as_of,
        training_year=rotation.training_year,
        rotation=rotation,
    )
    results = evaluate_many(ctx, rules)
    blocking = [r for r in results if not r.met and r.severity == RequirementSeverity.MANDATORY]
    met = sum(1 for r in results if r.met)
    percent = (met / len(results) * 100) if results else 100.0

    return {
        "rotation_id": rotation.id,
        "requirements": [r.to_dict() for r in results],
        "blocking": [r.to_dict() for r in blocking],
        "met": met,
        "total": len(results),
        "completion_percent": round(percent, 1),
        "can_complete": not blocking,
    }


def close_rotation(
    db: Session,
    rotation: RotationAssignment,
    *,
    closed_by_id: str,
    outcome: str = RotationStatus.COMPLETED,
    comment: str | None = None,
    force: bool = False,
) -> RotationAssignment:
    """Sign off a rotation. Refuses to mark it completed with mandatory gaps open
    unless a supervisor explicitly forces it (which is recorded)."""
    summary = evaluate_completion(db, rotation)
    if outcome == RotationStatus.COMPLETED and not summary["can_complete"] and not force:
        raise ValueError(
            f"{len(summary['blocking'])} mandatory rotation requirement(s) unmet. "
            "Resolve them or close with force=True and a supervisor comment."
        )

    rotation.status = outcome
    rotation.completion_summary = summary
    rotation.completion_percent = summary["completion_percent"]
    rotation.supervisor_comment = comment
    rotation.closed_at = utcnow()
    rotation.closed_by_id = closed_by_id
    db.add(rotation)
    return rotation


def extend_rotation(
    db: Session,
    rotation: RotationAssignment,
    *,
    new_end_date: date,
    reason: str,
    cascade: bool = True,
) -> list[RotationAssignment]:
    """Extend a rotation and push subsequent planned rotations back by the same period."""
    if new_end_date <= rotation.end_date:
        raise ValueError("The new end date must be later than the current end date.")

    shift_days = (new_end_date - rotation.end_date).days
    rotation.original_end_date = rotation.original_end_date or rotation.end_date
    rotation.end_date = new_end_date
    rotation.status = RotationStatus.EXTENDED
    rotation.extension_reason = reason
    db.add(rotation)
    touched = [rotation]

    if cascade:
        for other in rotation.enrolment.rotations:
            if other.id == rotation.id or other.start_date <= rotation.start_date:
                continue
            if other.status not in {RotationStatus.PLANNED, RotationStatus.ACTIVE}:
                continue
            other.start_date += timedelta(days=shift_days)
            other.end_date += timedelta(days=shift_days)
            db.add(other)
            touched.append(other)
    return touched


def create_remedial(
    db: Session,
    rotation: RotationAssignment,
    *,
    weeks: int,
    start_date: date | None = None,
    supervisor_id: str | None = None,
    reason: str | None = None,
) -> RotationAssignment:
    """Create a repeat posting that remediates a failed or incomplete rotation."""
    start = start_date or (rotation.end_date + timedelta(days=1))
    remedial = RotationAssignment(
        tenant_id=rotation.tenant_id,
        enrolment_id=rotation.enrolment_id,
        rotation_template_id=rotation.rotation_template_id,
        org_unit_id=rotation.org_unit_id,
        supervisor_id=supervisor_id or rotation.supervisor_id,
        name=f"{rotation.name} (remedial)",
        training_year=rotation.training_year,
        sequence=rotation.sequence,
        start_date=start,
        end_date=start + timedelta(weeks=weeks) - timedelta(days=1),
        status=RotationStatus.REMEDIAL,
        is_remedial=True,
        remediates_id=rotation.id,
        objectives=list(rotation.objectives or []),
        extension_reason=reason,
    )
    db.add(remedial)
    return remedial


def apply_leave_interruption(
    db: Session, leave: LeaveRecord, *, cascade: bool = True
) -> list[RotationAssignment]:
    """When approved leave interrupts training, shift the affected rotations.

    Only leave flagged ``extends_training`` moves the schedule; ordinary annual leave
    is taken within a posting and does not extend it.
    """
    if not leave.extends_training:
        return []

    enrolment = leave.enrolment
    shift_days = leave.days
    enrolment.interruption_days += shift_days
    enrolment.expected_end_date += timedelta(days=shift_days)
    db.add(enrolment)

    touched: list[RotationAssignment] = []
    if not cascade:
        return touched

    for rotation in enrolment.rotations:
        if rotation.end_date < leave.start_date:
            continue
        if rotation.status in {RotationStatus.COMPLETED, RotationStatus.CANCELLED}:
            continue
        if rotation.start_date <= leave.start_date <= rotation.end_date:
            rotation.end_date += timedelta(days=shift_days)
            rotation.status = RotationStatus.INTERRUPTED
        else:
            rotation.start_date += timedelta(days=shift_days)
            rotation.end_date += timedelta(days=shift_days)
        db.add(rotation)
        touched.append(rotation)
    return touched


def refresh_statuses(db: Session, tenant_id: str, *, on: date | None = None) -> dict[str, int]:
    """Advance rotation statuses with the calendar. Run daily by the scheduler."""
    on = on or date.today()
    started = db.execute(
        select(RotationAssignment).where(
            RotationAssignment.tenant_id == tenant_id,
            RotationAssignment.status == RotationStatus.PLANNED,
            RotationAssignment.start_date <= on,
            RotationAssignment.end_date >= on,
        )
    ).scalars().all()
    for rotation in started:
        rotation.status = RotationStatus.ACTIVE
        db.add(rotation)

    overdue = db.execute(
        select(RotationAssignment).where(
            RotationAssignment.tenant_id == tenant_id,
            RotationAssignment.status.in_([RotationStatus.ACTIVE, RotationStatus.EXTENDED]),
            RotationAssignment.end_date < on,
        )
    ).scalars().all()

    return {
        "activated": len(started),
        "awaiting_signoff": len(overdue),
    }
