"""Role-based access control catalogue.

Permissions are declared here as the *canonical vocabulary*; roles are stored in the
database so institutions can create their own without a code change. The seeder maps
these permission codes onto the default role set described in the specification.

Permission code convention:  ``<domain>.<resource>.<action>``
Scope: every role assignment is bound to an ``OrgUnit`` — a permission held at
``Department/Surgery`` does not grant access to ``Department/Paediatrics``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PermissionCategory(StrEnum):
    PLATFORM = "platform"
    TENANCY = "tenancy"
    IDENTITY = "identity"
    CURRICULUM = "curriculum"
    TRAINING = "training"
    DUTY = "duty"
    LOGBOOK = "logbook"
    ASSESSMENT = "assessment"
    ACADEMIC = "academic"
    EXAM = "exam"
    CME = "cme"
    RESEARCH = "research"
    ANALYTICS = "analytics"
    PROMOTION = "promotion"
    ACCREDITATION = "accreditation"
    REPORTING = "reporting"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class PermissionSpec:
    code: str
    name: str
    category: PermissionCategory


def _p(code: str, name: str, category: PermissionCategory) -> PermissionSpec:
    return PermissionSpec(code=code, name=name, category=category)


C = PermissionCategory

PERMISSIONS: tuple[PermissionSpec, ...] = (
    # -- platform ---------------------------------------------------------
    _p("platform.tenant.manage", "Create and configure institutions", C.PLATFORM),
    _p("platform.national.view", "View national roll-up analytics", C.PLATFORM),
    _p("platform.settings.manage", "Manage platform-wide settings", C.PLATFORM),
    # -- tenancy ----------------------------------------------------------
    _p("tenancy.orgunit.read", "View organisational units", C.TENANCY),
    _p("tenancy.orgunit.manage", "Create/edit faculties, departments, units", C.TENANCY),
    _p("tenancy.settings.manage", "Manage institution settings and branding", C.TENANCY),
    # -- identity ---------------------------------------------------------
    _p("identity.user.read", "View user directory", C.IDENTITY),
    _p("identity.user.manage", "Create, edit, deactivate users", C.IDENTITY),
    _p("identity.role.manage", "Define roles and assign permissions", C.IDENTITY),
    _p("identity.assignment.manage", "Assign roles to users within a scope", C.IDENTITY),
    # -- curriculum -------------------------------------------------------
    _p("curriculum.read", "View curricula", C.CURRICULUM),
    _p("curriculum.manage", "Build and edit curricula", C.CURRICULUM),
    _p("curriculum.publish", "Publish/approve a curriculum version", C.CURRICULUM),
    _p("curriculum.specialty.manage", "Create specialties and subspecialties", C.CURRICULUM),
    # -- training ---------------------------------------------------------
    _p("training.enrolment.read", "View enrolments", C.TRAINING),
    _p("training.enrolment.manage", "Enrol and transfer trainees", C.TRAINING),
    _p("training.rotation.read", "View rotation assignments", C.TRAINING),
    _p("training.rotation.manage", "Assign, extend and close rotations", C.TRAINING),
    _p("training.leave.approve", "Approve leave and interruptions", C.TRAINING),
    # -- duty -------------------------------------------------------------
    _p("duty.roster.read", "View duty rosters", C.DUTY),
    _p("duty.roster.manage", "Generate and publish duty rosters", C.DUTY),
    _p("duty.swap.approve", "Approve duty swap requests", C.DUTY),
    _p("duty.attendance.record", "Record attendance", C.DUTY),
    # -- logbook ----------------------------------------------------------
    _p("logbook.entry.create", "Create own logbook entries", C.LOGBOOK),
    _p("logbook.entry.read.own", "Read own logbook", C.LOGBOOK),
    _p("logbook.entry.read.supervised", "Read logbooks of supervised trainees", C.LOGBOOK),
    _p("logbook.entry.read.any", "Read any logbook within scope", C.LOGBOOK),
    _p("logbook.entry.validate", "Validate/counter-sign logbook entries", C.LOGBOOK),
    _p("logbook.catalogue.manage", "Manage the procedure catalogue", C.LOGBOOK),
    # -- assessment -------------------------------------------------------
    _p("assessment.template.manage", "Design assessment instruments", C.ASSESSMENT),
    _p("assessment.submit", "Complete an assessment of a trainee", C.ASSESSMENT),
    _p("assessment.read.own", "Read own assessments", C.ASSESSMENT),
    _p("assessment.read.any", "Read any assessment within scope", C.ASSESSMENT),
    _p("assessment.competency.rate", "Award competency / EPA entrustment levels", C.ASSESSMENT),
    # -- academic ---------------------------------------------------------
    _p("academic.activity.read", "View academic activity calendar", C.ACADEMIC),
    _p("academic.activity.manage", "Schedule academic activities", C.ACADEMIC),
    _p("academic.attendance.record", "Record academic attendance", C.ACADEMIC),
    # -- exams / CBT ------------------------------------------------------
    _p("exam.bank.manage", "Author and curate question banks", C.EXAM),
    _p("exam.paper.manage", "Assemble and schedule papers", C.EXAM),
    _p("exam.attempt.take", "Sit examinations", C.EXAM),
    _p("exam.result.read.any", "View any candidate results within scope", C.EXAM),
    # -- CME --------------------------------------------------------------
    _p("cme.resource.manage", "Curate CME resources", C.CME),
    _p("cme.assignment.manage", "Assign CME to trainees", C.CME),
    _p("cme.assignment.complete", "Complete assigned CME", C.CME),
    # -- research ---------------------------------------------------------
    _p("research.project.create", "Register a research project or dissertation", C.RESEARCH),
    _p("research.project.read.any", "Read any research project within scope", C.RESEARCH),
    _p("research.supervise", "Supervise dissertations", C.RESEARCH),
    _p("research.milestone.approve", "Approve dissertation milestones", C.RESEARCH),
    _p("research.ethics.review", "Review and record ethics approval", C.RESEARCH),
    # -- analytics --------------------------------------------------------
    _p("analytics.self.read", "View own performance analytics", C.ANALYTICS),
    _p("analytics.supervised.read", "View analytics for supervised trainees", C.ANALYTICS),
    _p("analytics.department.read", "View department analytics", C.ANALYTICS),
    _p("analytics.institution.read", "View institution-wide analytics", C.ANALYTICS),
    # -- promotion --------------------------------------------------------
    _p("promotion.readiness.read", "View promotion readiness", C.PROMOTION),
    _p("promotion.decide", "Record a promotion decision", C.PROMOTION),
    # -- accreditation ----------------------------------------------------
    _p("accreditation.profile.manage", "Define accreditation requirement profiles", C.ACCREDITATION),
    _p("accreditation.report.generate", "Generate accreditation returns", C.ACCREDITATION),
    _p("accreditation.evidence.manage", "Upload and manage accreditation evidence", C.ACCREDITATION),
    # -- reporting --------------------------------------------------------
    _p("reporting.export", "Export reports (PDF/XLSX/CSV/DOCX)", C.REPORTING),
    _p("reporting.portfolio.read.any", "Read any trainee portfolio within scope", C.REPORTING),
    # -- system -----------------------------------------------------------
    _p("system.audit.read", "Read audit trails", C.SYSTEM),
    _p("system.notification.manage", "Manage notification rules and templates", C.SYSTEM),
    _p("system.sync", "Use the offline synchronisation endpoints", C.SYSTEM),
)

PERMISSION_CODES: frozenset[str] = frozenset(p.code for p in PERMISSIONS)

# Wildcard granted to the National Super Administrator.
SUPERUSER_WILDCARD = "*"


class OrgKind(StrEnum):
    """The eight-level tenancy ladder from the specification."""

    NATIONAL = "national"
    COLLEGE = "college"
    HOSPITAL = "hospital"
    FACULTY = "faculty"
    DEPARTMENT = "department"
    UNIT = "unit"
    SUBSPECIALTY = "subspecialty"
    PROGRAMME = "programme"


ORG_KIND_ORDER: tuple[OrgKind, ...] = (
    OrgKind.NATIONAL,
    OrgKind.COLLEGE,
    OrgKind.HOSPITAL,
    OrgKind.FACULTY,
    OrgKind.DEPARTMENT,
    OrgKind.UNIT,
    OrgKind.SUBSPECIALTY,
    OrgKind.PROGRAMME,
)


# --------------------------------------------------------------------------
# Default role catalogue — seeded, then editable by each institution.
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RoleSpec:
    code: str
    name: str
    rank: int  # lower number == more senior; used for delegation checks
    scope_kind: OrgKind
    permissions: tuple[str, ...]


_TRAINEE_BASE = (
    "tenancy.orgunit.read",
    "curriculum.read",
    "training.enrolment.read",
    "training.rotation.read",
    "duty.roster.read",
    "logbook.entry.create",
    "logbook.entry.read.own",
    "assessment.read.own",
    "academic.activity.read",
    "exam.attempt.take",
    "cme.assignment.complete",
    "research.project.create",
    "analytics.self.read",
    "promotion.readiness.read",
    "reporting.export",
    "system.sync",
)

_FACULTY_BASE = _TRAINEE_BASE + (
    "identity.user.read",
    "training.rotation.read",
    "logbook.entry.read.supervised",
    "logbook.entry.validate",
    "assessment.submit",
    "assessment.competency.rate",
    "assessment.read.any",
    "academic.attendance.record",
    "analytics.supervised.read",
    "research.supervise",
    "research.milestone.approve",
    "research.project.read.any",
    "duty.attendance.record",
)

_DEPARTMENT_LEADERSHIP = _FACULTY_BASE + (
    "tenancy.orgunit.manage",
    "curriculum.manage",
    "training.enrolment.manage",
    "training.rotation.manage",
    "training.leave.approve",
    "duty.roster.manage",
    "duty.swap.approve",
    "assessment.template.manage",
    "academic.activity.manage",
    "exam.bank.manage",
    "exam.paper.manage",
    "exam.result.read.any",
    "cme.resource.manage",
    "cme.assignment.manage",
    "logbook.catalogue.manage",
    "analytics.department.read",
    "promotion.decide",
    "accreditation.report.generate",
    "accreditation.evidence.manage",
    "reporting.portfolio.read.any",
    "logbook.entry.read.any",
)

_INSTITUTION_LEADERSHIP = _DEPARTMENT_LEADERSHIP + (
    "identity.user.manage",
    "identity.assignment.manage",
    # Institutions define their own roles; without this no shipped role could, and
    # the "configure, don't code" promise would fail at the first custom role.
    "identity.role.manage",
    "tenancy.settings.manage",
    "curriculum.publish",
    "curriculum.specialty.manage",
    "analytics.institution.read",
    "accreditation.profile.manage",
    "system.audit.read",
    "system.notification.manage",
)

DEFAULT_ROLES: tuple[RoleSpec, ...] = (
    RoleSpec("national_super_admin", "National Super Administrator", 0, OrgKind.NATIONAL, (SUPERUSER_WILDCARD,)),
    RoleSpec("national_residency_admin", "National Residency Administrator", 5, OrgKind.NATIONAL,
             _INSTITUTION_LEADERSHIP + ("platform.national.view", "platform.tenant.manage")),
    RoleSpec("college_admin", "College Administrator", 10, OrgKind.COLLEGE,
             _INSTITUTION_LEADERSHIP + ("platform.national.view",)),
    RoleSpec("chief_medical_director", "Chief Medical Director", 15, OrgKind.HOSPITAL, _INSTITUTION_LEADERSHIP),
    RoleSpec("medical_director", "Medical Director", 16, OrgKind.HOSPITAL, _INSTITUTION_LEADERSHIP),
    RoleSpec("cmac", "Chairman, Medical Advisory Committee", 17, OrgKind.HOSPITAL, _INSTITUTION_LEADERSHIP),
    RoleSpec("director_residency", "Director of Residency Training", 20, OrgKind.HOSPITAL, _INSTITUTION_LEADERSHIP),
    RoleSpec("deputy_director_residency", "Deputy Director of Residency Training", 21, OrgKind.HOSPITAL,
             _DEPARTMENT_LEADERSHIP + ("analytics.institution.read",)),
    RoleSpec("dean", "Dean", 22, OrgKind.FACULTY, _INSTITUTION_LEADERSHIP),
    RoleSpec("head_of_department", "Head of Department", 25, OrgKind.DEPARTMENT, _DEPARTMENT_LEADERSHIP),
    RoleSpec("residency_coordinator", "Department Residency Coordinator", 30, OrgKind.DEPARTMENT,
             _DEPARTMENT_LEADERSHIP),
    RoleSpec("training_coordinator", "Training Coordinator", 31, OrgKind.DEPARTMENT,
             _FACULTY_BASE + ("training.rotation.manage", "duty.roster.manage", "academic.activity.manage",
                              "analytics.department.read")),
    RoleSpec("consultant", "Consultant", 35, OrgKind.DEPARTMENT, _FACULTY_BASE),
    RoleSpec("intern_supervisor", "Intern Supervisor", 36, OrgKind.DEPARTMENT, _FACULTY_BASE),
    RoleSpec("research_supervisor", "Research Supervisor", 37, OrgKind.DEPARTMENT,
             _FACULTY_BASE + ("research.ethics.review",)),
    RoleSpec("senior_registrar", "Senior Registrar", 45, OrgKind.DEPARTMENT,
             _TRAINEE_BASE + ("logbook.entry.read.supervised", "academic.attendance.record")),
    RoleSpec("registrar", "Registrar", 50, OrgKind.DEPARTMENT, _TRAINEE_BASE),
    RoleSpec("resident", "Resident", 51, OrgKind.DEPARTMENT, _TRAINEE_BASE),
    RoleSpec("medical_officer", "Medical Officer", 55, OrgKind.DEPARTMENT, _TRAINEE_BASE),
    RoleSpec("dental_officer", "Dental Officer", 56, OrgKind.DEPARTMENT, _TRAINEE_BASE),
    RoleSpec("house_officer", "House Officer", 60, OrgKind.DEPARTMENT, _TRAINEE_BASE),
    RoleSpec("intern", "Intern", 61, OrgKind.DEPARTMENT, _TRAINEE_BASE),
    RoleSpec("secretary", "Secretary", 65, OrgKind.DEPARTMENT,
             ("tenancy.orgunit.read", "identity.user.read", "academic.activity.manage",
              "academic.attendance.record", "duty.roster.read", "reporting.export")),
    RoleSpec("quality_assurance", "Quality Assurance Officer", 40, OrgKind.HOSPITAL,
             ("tenancy.orgunit.read", "curriculum.read", "analytics.institution.read",
              "analytics.department.read", "accreditation.report.generate", "system.audit.read",
              "reporting.export", "reporting.portfolio.read.any")),
    RoleSpec("external_examiner", "External Examiner", 41, OrgKind.DEPARTMENT,
             ("curriculum.read", "exam.result.read.any", "assessment.read.any",
              "reporting.portfolio.read.any", "logbook.entry.read.any")),
    RoleSpec("accreditation_team", "Accreditation Team Member", 42, OrgKind.HOSPITAL,
             ("tenancy.orgunit.read", "curriculum.read", "analytics.department.read",
              "analytics.institution.read", "accreditation.report.generate",
              "reporting.export", "reporting.portfolio.read.any")),
    RoleSpec("observer", "Observer", 80, OrgKind.DEPARTMENT,
             ("tenancy.orgunit.read", "curriculum.read", "academic.activity.read")),
    RoleSpec("guest", "Guest", 90, OrgKind.HOSPITAL, ("tenancy.orgunit.read", "academic.activity.read")),
)

TRAINEE_ROLE_CODES: frozenset[str] = frozenset(
    {"senior_registrar", "registrar", "resident", "house_officer", "intern",
     "medical_officer", "dental_officer"}
)

SUPERVISOR_ROLE_CODES: frozenset[str] = frozenset(
    {"consultant", "intern_supervisor", "research_supervisor", "head_of_department",
     "residency_coordinator", "training_coordinator", "senior_registrar"}
)


def validate_catalogue() -> None:
    """Fail fast if a role references an unknown permission code."""
    unknown: set[str] = set()
    for role in DEFAULT_ROLES:
        for code in role.permissions:
            if code != SUPERUSER_WILDCARD and code not in PERMISSION_CODES:
                unknown.add(f"{role.code}:{code}")
    if unknown:
        raise RuntimeError(f"Unknown permission codes in role catalogue: {sorted(unknown)}")


validate_catalogue()
