"""Supervisor allocation.

Two related problems:

* **Clinical supervision** — who supervises this trainee on this rotation?
* **Research supervision** — who supervises this dissertation?

Both are solved by transparent, weighted scoring over the criteria the specification
names: expertise match, availability, current workload, previous supervision history,
declared conflicts of interest, and a hard cap on supervisees. Every allocation carries
its own explanation, so a trainee or HOD can see exactly why a pairing was proposed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.rbac import SUPERVISOR_ROLE_CODES
from app.models.enums import RotationStatus
from app.models.identity import Role, RoleAssignment, SupervisorProfile, User
from app.models.research import ProjectSupervision, ResearchProject
from app.models.tenancy import OrgUnit
from app.models.training import RotationAssignment

#: Relative importance of each criterion. Institutions may override these in
#: ``Tenant.settings["allocation_weights"]``.
DEFAULT_RESEARCH_WEIGHTS: dict[str, float] = {
    "expertise": 0.40,
    "capacity": 0.25,
    "track_record": 0.20,
    "availability": 0.15,
}

DEFAULT_CLINICAL_WEIGHTS: dict[str, float] = {
    "workload": 0.45,
    "same_unit": 0.30,
    "continuity": 0.15,
    "availability": 0.10,
}


@dataclass(slots=True)
class Candidate:
    user_id: str
    name: str
    total_score: float
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    current_load: int = 0
    capacity: int = 0
    eligible: bool = True
    exclusion_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "score": round(self.total_score, 3),
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "reasons": self.reasons,
            "current_load": self.current_load,
            "capacity": self.capacity,
            "eligible": self.eligible,
            "exclusion_reason": self.exclusion_reason,
        }


# --------------------------------------------------------------------------
# Candidate pools
# --------------------------------------------------------------------------
def supervisor_pool(
    db: Session, *, tenant_id: str, org_unit_id: str | None = None
) -> list[User]:
    """Users holding a supervisor-capable role, optionally within an org subtree."""
    stmt = (
        select(User)
        .join(RoleAssignment, RoleAssignment.user_id == User.id)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(
            User.tenant_id == tenant_id,
            User.status == "active",
            User.deleted_at.is_(None),
            Role.code.in_(tuple(SUPERVISOR_ROLE_CODES)),
        )
        .distinct()
    )

    if org_unit_id:
        unit = db.get(OrgUnit, org_unit_id)
        if unit is not None:
            # A supervisor assigned at a parent (e.g. Department) covers its children.
            covering_ids = [
                u.id
                for u in db.execute(
                    select(OrgUnit).where(
                        OrgUnit.tenant_id == tenant_id,
                        OrgUnit.id.in_(_ancestor_ids(db, unit)),
                    )
                ).scalars()
            ]
            stmt = stmt.where(RoleAssignment.org_unit_id.in_(covering_ids + [org_unit_id]))

    return list(db.execute(stmt).scalars().all())


def _ancestor_ids(db: Session, unit: OrgUnit) -> list[str]:
    """Ids of every ancestor of ``unit``, derived from its materialised path."""
    codes = [segment for segment in (unit.path or "").split("/") if segment]
    if not codes:
        return [unit.id]
    rows = db.execute(
        select(OrgUnit.id).where(OrgUnit.tenant_id == unit.tenant_id, OrgUnit.code.in_(codes))
    ).scalars().all()
    return list(rows)


# --------------------------------------------------------------------------
# Load measurement
# --------------------------------------------------------------------------
def clinical_load(db: Session, supervisor_id: str, *, on: date) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(RotationAssignment)
            .where(
                RotationAssignment.supervisor_id == supervisor_id,
                RotationAssignment.status.in_(
                    [RotationStatus.ACTIVE, RotationStatus.EXTENDED, RotationStatus.REMEDIAL]
                ),
                RotationAssignment.start_date <= on,
                RotationAssignment.end_date >= on,
            )
        ).scalar_one()
    )


def research_load(db: Session, supervisor_id: str) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(ProjectSupervision)
            .join(ResearchProject, ResearchProject.id == ProjectSupervision.project_id)
            .where(
                ProjectSupervision.supervisor_id == supervisor_id,
                ProjectSupervision.ended_on.is_(None),
                ResearchProject.completed_on.is_(None),
                ResearchProject.deleted_at.is_(None),
            )
        ).scalar_one()
    )


def _profile_for(db: Session, user_id: str) -> SupervisorProfile | None:
    return db.execute(
        select(SupervisorProfile).where(SupervisorProfile.user_id == user_id)
    ).scalar_one_or_none()


# --------------------------------------------------------------------------
# Research supervisor allocation
# --------------------------------------------------------------------------
def _expertise_overlap(profile: SupervisorProfile | None, keywords: list[str]) -> tuple[float, list[str]]:
    """Jaccard-style overlap between project keywords and declared expertise."""
    if not profile or not keywords:
        return 0.0, []
    expertise = {e.lower().strip() for e in (profile.expertise or [])}
    methods = {m.lower().strip() for m in (profile.methodologies or [])}
    wanted = {k.lower().strip() for k in keywords}
    if not expertise and not methods:
        return 0.0, []

    direct = wanted & expertise
    method = wanted & methods
    # Partial credit for substring matches ("paediatric surgery" vs "surgery").
    partial = {
        w for w in wanted - direct
        if any(w in e or e in w for e in expertise)
    }
    matched = direct | method | partial
    score = min(1.0, (len(direct) * 1.0 + len(method) * 0.8 + len(partial) * 0.5) / len(wanted))
    return score, sorted(matched)


def rank_research_supervisors(
    db: Session,
    *,
    tenant_id: str,
    org_unit_id: str,
    trainee_id: str,
    keywords: list[str],
    on: date | None = None,
    weights: dict[str, float] | None = None,
    include_ineligible: bool = False,
) -> list[Candidate]:
    """Rank potential dissertation supervisors, best first."""
    on = on or date.today()
    w = {**DEFAULT_RESEARCH_WEIGHTS, **(weights or {})}
    candidates: list[Candidate] = []

    for user in supervisor_pool(db, tenant_id=tenant_id, org_unit_id=org_unit_id):
        if user.id == trainee_id:
            continue
        profile = _profile_for(db, user.id)
        load = research_load(db, user.id)
        capacity = profile.max_supervisees if profile else 5

        candidate = Candidate(
            user_id=user.id,
            name=user.full_name,
            total_score=0.0,
            current_load=load,
            capacity=capacity,
        )

        # -- hard exclusions ----------------------------------------------
        if profile and trainee_id in (profile.conflicts_of_interest or []):
            candidate.eligible = False
            candidate.exclusion_reason = "Declared conflict of interest with this trainee."
        elif load >= capacity:
            candidate.eligible = False
            candidate.exclusion_reason = f"At maximum supervision load ({load}/{capacity})."
        elif profile and not profile.is_available_on(on):
            candidate.eligible = False
            candidate.exclusion_reason = "Not currently accepting new supervisees."

        # -- scoring -------------------------------------------------------
        expertise_score, matched = _expertise_overlap(profile, keywords)
        capacity_score = 1.0 - (load / capacity) if capacity else 0.0
        completed = profile.completed_supervisions if profile else 0
        # Diminishing returns: experience helps, but saturates around six completions.
        track_record = min(1.0, completed / 6.0)
        availability = 1.0 if (profile is None or profile.is_available_on(on)) else 0.0

        candidate.components = {
            "expertise": expertise_score,
            "capacity": max(0.0, capacity_score),
            "track_record": track_record,
            "availability": availability,
        }
        candidate.total_score = sum(
            candidate.components[k] * w.get(k, 0.0) for k in candidate.components
        )

        if matched:
            candidate.reasons.append("Expertise match: " + ", ".join(matched))
        elif keywords:
            candidate.reasons.append("No declared expertise overlap with the project keywords.")
        candidate.reasons.append(f"Supervising {load} of a maximum {capacity}.")
        if completed:
            candidate.reasons.append(f"{completed} completed supervision(s) on record.")
        if candidate.exclusion_reason:
            candidate.reasons.append(candidate.exclusion_reason)

        candidates.append(candidate)

    pool = candidates if include_ineligible else [c for c in candidates if c.eligible]
    pool.sort(key=lambda c: c.total_score, reverse=True)
    return pool


# --------------------------------------------------------------------------
# Clinical supervisor allocation
# --------------------------------------------------------------------------
def rank_clinical_supervisors(
    db: Session,
    *,
    tenant_id: str,
    org_unit_id: str,
    trainee_id: str,
    on: date | None = None,
    weights: dict[str, float] | None = None,
) -> list[Candidate]:
    """Rank consultants for a rotation posting, favouring an even workload spread."""
    on = on or date.today()
    w = {**DEFAULT_CLINICAL_WEIGHTS, **(weights or {})}
    candidates: list[Candidate] = []

    pool = supervisor_pool(db, tenant_id=tenant_id, org_unit_id=org_unit_id)
    if not pool:
        # Fall back to the whole institution rather than returning nothing — a posting
        # with no supervisor at all is worse than one supervised from the parent unit.
        pool = supervisor_pool(db, tenant_id=tenant_id)

    loads = {user.id: clinical_load(db, user.id, on=on) for user in pool}
    max_load = max(loads.values(), default=0)

    prior = {
        row[0]: row[1]
        for row in db.execute(
            select(RotationAssignment.supervisor_id, func.count())
            .join(RotationAssignment.enrolment)
            .where(RotationAssignment.supervisor_id.is_not(None))
            .group_by(RotationAssignment.supervisor_id)
        ).all()
    }

    for user in pool:
        if user.id == trainee_id:
            continue
        profile = _profile_for(db, user.id)
        load = loads[user.id]
        capacity = profile.max_clinical_trainees if profile else 8

        candidate = Candidate(
            user_id=user.id,
            name=user.full_name,
            total_score=0.0,
            current_load=load,
            capacity=capacity,
        )

        if profile and trainee_id in (profile.conflicts_of_interest or []):
            candidate.eligible = False
            candidate.exclusion_reason = "Declared conflict of interest."
        elif load >= capacity:
            candidate.eligible = False
            candidate.exclusion_reason = f"At maximum clinical load ({load}/{capacity})."

        workload_score = 1.0 - (load / max(1, max_load)) if max_load else 1.0
        same_unit = 1.0 if _supervises_unit(db, user.id, org_unit_id) else 0.4
        # Continuity is mildly *negative*: spreading trainees across supervisors gives
        # broader exposure, which is what training committees generally want.
        continuity = 1.0 - min(1.0, prior.get(user.id, 0) / 10.0)
        availability = 1.0 if (profile is None or profile.is_available_on(on)) else 0.0

        candidate.components = {
            "workload": max(0.0, workload_score),
            "same_unit": same_unit,
            "continuity": continuity,
            "availability": availability,
        }
        candidate.total_score = sum(
            candidate.components[k] * w.get(k, 0.0) for k in candidate.components
        )
        candidate.reasons.append(f"Currently supervising {load} trainee(s) (cap {capacity}).")
        if same_unit == 1.0:
            candidate.reasons.append("Holds a supervisory role in the receiving unit.")
        if candidate.exclusion_reason:
            candidate.reasons.append(candidate.exclusion_reason)

        candidates.append(candidate)

    eligible = [c for c in candidates if c.eligible]
    eligible.sort(key=lambda c: c.total_score, reverse=True)
    return eligible


def _supervises_unit(db: Session, user_id: str, org_unit_id: str) -> bool:
    return (
        db.execute(
            select(func.count())
            .select_from(RoleAssignment)
            .where(
                RoleAssignment.user_id == user_id,
                RoleAssignment.org_unit_id == org_unit_id,
            )
        ).scalar_one()
        > 0
    )


# --------------------------------------------------------------------------
# Assignment
# --------------------------------------------------------------------------
def assign_research_supervisor(
    db: Session,
    project: ResearchProject,
    *,
    supervisor_id: str | None = None,
    is_primary: bool = True,
    allocation_method: str = "automatic",
) -> ProjectSupervision:
    """Attach a supervisor to a project, automatically choosing the best fit if none
    is named. Raises when no eligible supervisor exists rather than assigning blindly."""
    rationale: dict[str, Any] = {}

    if supervisor_id is None:
        ranked = rank_research_supervisors(
            db,
            tenant_id=project.tenant_id,
            org_unit_id=project.org_unit_id,
            trainee_id=project.principal_investigator_id,
            keywords=list(project.keywords or []),
        )
        if not ranked:
            raise ValueError(
                "No eligible research supervisor is available in this department. "
                "Increase supervision caps, clear conflicts of interest, or assign manually."
            )
        best = ranked[0]
        supervisor_id = best.user_id
        rationale = {
            "chosen": best.as_dict(),
            "runners_up": [c.as_dict() for c in ranked[1:4]],
            "method": "weighted_criteria",
            "weights": DEFAULT_RESEARCH_WEIGHTS,
        }

    supervision = ProjectSupervision(
        tenant_id=project.tenant_id,
        project_id=project.id,
        supervisor_id=supervisor_id,
        is_primary=is_primary,
        assigned_on=date.today(),
        allocation_method=allocation_method,
        allocation_score=rationale,
    )
    db.add(supervision)
    return supervision
