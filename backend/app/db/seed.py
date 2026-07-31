"""Database seeding.

``python -m app.db.seed`` creates the schema, loads platform reference data and builds a
complete demo institution: departments, faculty, trainees, curricula with real
requirement rules, a year of logbook and academic activity, assessments, research
projects, computed scorecards and accreditation profiles.

The seeder is idempotent — running it twice will not duplicate anything — and refuses to
run against a production environment.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, time, timedelta

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.rbac import (
    DEFAULT_ROLES,
    PERMISSIONS,
    SUPERVISOR_ROLE_CODES,
    TRAINEE_ROLE_CODES,
)
from app.core.security import hash_password
from app.db.base import Base, utcnow
from app.db.reference import ACCREDITATION_PROFILES, PROCEDURES, SPECIALTIES
from app.db.session import SessionLocal, engine
from app.models.academic import AcademicActivity, ActivityParticipant
from app.models.analytics import AccreditationCriterion, AccreditationProfile
from app.models.assessment import Assessment, AssessmentTemplate, CompetencyRating
from app.models.cme import CmeCreditLedger
from app.models.curriculum import (
    Competency,
    CurriculumVersion,
    ProcedureCatalogueItem,
    Programme,
    RequirementRule,
    RotationTemplate,
    Specialty,
    TrainingYear,
)
from app.models.duty import AttendanceRecord
from app.models.enums import (
    ENTRUSTMENT_ORDER,
    AcademicActivityKind,
    AssessmentKind,
    AttendanceStatus,
    CaseComplexity,
    CaseOutcome,
    CompetencyDomain,
    CurriculumStatus,
    DissertationStage,
    EntrustmentLevel,
    LogEntryType,
    ParticipantRole,
    ParticipationRole,
    ProgrammeType,
    RequirementKind,
    RequirementScope,
    RequirementSeverity,
    ScoreDomain,
    TrainingLevel,
    UserStatus,
    ValidationStatus,
)
from app.models.identity import Permission, Role, RoleAssignment, SupervisorProfile, User
from app.models.logbook import LogEntry, TeachingRecord
from app.models.research import (
    DissertationMilestone,
    ProjectSupervision,
    Publication,
    ResearchProject,
)
from app.models.tenancy import OrgUnit, Tenant
from app.models.training import Enrolment
from app.services import rotation as rotation_engine
from app.services import scoring

DEMO_PASSWORD = "RtcDemo!2026"
DEMO_TENANT_CODE = "UTH-DEMO"

rng = random.Random(20260731)


def log(message: str) -> None:
    print(f"  · {message}")


# ==========================================================================
# Platform reference data
# ==========================================================================
def seed_permissions(db: Session) -> None:
    existing = {p.code for p in db.execute(select(Permission)).scalars()}
    created = 0
    for spec in PERMISSIONS:
        if spec.code in existing:
            continue
        db.add(Permission(code=spec.code, name=spec.name, category=spec.category))
        created += 1
    db.flush()
    log(f"permissions: {created} created, {len(existing)} already present")


def seed_roles(db: Session) -> dict[str, Role]:
    roles: dict[str, Role] = {
        r.code: r
        for r in db.execute(select(Role).where(Role.tenant_id.is_(None))).scalars()
    }
    created = 0
    for spec in DEFAULT_ROLES:
        role = roles.get(spec.code)
        if role is None:
            role = Role(
                tenant_id=None,
                code=spec.code,
                name=spec.name,
                rank=spec.rank,
                scope_kind=spec.scope_kind,
                is_system=True,
                is_trainee_role=spec.code in TRAINEE_ROLE_CODES,
                is_supervisor_role=spec.code in SUPERVISOR_ROLE_CODES,
                permission_codes=list(spec.permissions),
            )
            db.add(role)
            roles[spec.code] = role
            created += 1
        else:
            # Keep shipped roles in step with the catalogue on upgrade.
            role.permission_codes = list(spec.permissions)
            role.name = spec.name
            role.rank = spec.rank
    db.flush()
    log(f"roles: {created} created, {len(DEFAULT_ROLES) - created} refreshed")
    return roles


def seed_specialties(db: Session) -> dict[str, Specialty]:
    existing = {
        s.code: s
        for s in db.execute(select(Specialty).where(Specialty.tenant_id.is_(None))).scalars()
    }
    created = 0
    for order, (code, name, group, discipline, parent_code) in enumerate(SPECIALTIES):
        if code in existing:
            continue
        specialty = Specialty(
            tenant_id=None,
            code=code,
            name=name,
            faculty_group=group,
            discipline=discipline,
            is_subspecialty=parent_code is not None,
            recognised_by=["npmcn", "wacs"] if group == "Surgery" else ["npmcn", "wacp"],
            sort_order=order,
        )
        db.add(specialty)
        existing[code] = specialty
        created += 1
    db.flush()

    # Second pass so parents exist before children are linked.
    for code, _, _, _, parent_code in SPECIALTIES:
        if parent_code and code in existing and parent_code in existing:
            existing[code].parent_id = existing[parent_code].id
    db.flush()
    log(f"specialties: {created} created ({len(existing)} total)")
    return existing


def seed_accreditation_profiles(db: Session) -> list[AccreditationProfile]:
    profiles = []
    for spec in ACCREDITATION_PROFILES:
        existing = db.execute(
            select(AccreditationProfile).where(
                AccreditationProfile.code == spec["code"],
                AccreditationProfile.tenant_id.is_(None),
            )
        ).scalar_one_or_none()
        if existing is not None:
            profiles.append(existing)
            continue

        profile = AccreditationProfile(
            tenant_id=None,
            body=spec["body"],
            body_name=spec["body_name"],
            code=spec["code"],
            name=spec["name"],
            version=spec["version"],
            description=spec.get("description"),
            applies_to_programme_types=spec.get("applies_to_programme_types", []),
        )
        db.add(profile)
        db.flush()
        for index, criterion in enumerate(spec["criteria"]):
            db.add(
                AccreditationCriterion(
                    tenant_id=None,
                    profile_id=profile.id,
                    section=criterion.get("section", "general"),
                    code=criterion["code"],
                    title=criterion["title"],
                    description=criterion.get("description"),
                    metric=criterion["metric"],
                    operator=criterion.get("operator", "gte"),
                    target_value=float(criterion["target_value"]),
                    unit=criterion.get("unit"),
                    parameters=criterion.get("parameters", {}),
                    weighting=criterion.get("weighting", "essential"),
                    evidence_guidance=criterion.get("evidence_guidance"),
                    sort_order=index,
                )
            )
        profiles.append(profile)
    db.flush()
    log(f"accreditation profiles: {len(profiles)} available")
    return profiles


# ==========================================================================
# Demo institution
# ==========================================================================
def build_tenant(db: Session) -> Tenant:
    tenant = db.execute(
        select(Tenant).where(Tenant.code == DEMO_TENANT_CODE)
    ).scalar_one_or_none()
    if tenant is not None:
        return tenant

    tenant = Tenant(
        name="University Teaching Hospital (Demo)",
        code=DEMO_TENANT_CODE,
        slug="uth-demo",
        kind="hospital",
        country="NG",
        state="Lagos",
        city="Lagos",
        address="1 Residency Way, Idi-Araba, Lagos",
        timezone="Africa/Lagos",
        contact_email="training@uthdemo.health",
        contact_phone="+234 800 000 0000",
        website="https://uthdemo.health",
        accrediting_bodies=["npmcn", "wacs", "wacp", "mdcn", "nuc"],
        branding={
            "primary": "#166534",
            "accent": "#b45309",
            "logo_text": "UTH",
            "motto": "Learn. Serve. Advance.",
        },
        settings={
            "academic_year_start_month": 7,
            "logbook_validation_sla_days": 7,
            "minimum_academic_attendance_percent": 75,
            "duty_hours_cap_per_week": 72,
            "promotion_committee_quorum": 5,
            "allow_self_checkin": True,
            "geo_fence_metres": 300,
        },
        is_active=True,
    )
    db.add(tenant)
    db.flush()
    log(f"tenant: {tenant.name} [{tenant.code}]")
    return tenant


def build_org_tree(db: Session, tenant: Tenant, specialties: dict[str, Specialty]) -> dict[str, OrgUnit]:
    """Hospital → Faculty → Department → Unit, with declared infrastructure capacity."""
    units: dict[str, OrgUnit] = {
        u.code: u
        for u in db.execute(select(OrgUnit).where(OrgUnit.tenant_id == tenant.id)).scalars()
    }
    if units:
        return units

    def add(code: str, name: str, kind: str, parent: OrgUnit | None,
            specialty_code: str | None = None, capacity: dict | None = None,
            discipline: str = "medical") -> OrgUnit:
        unit = OrgUnit(
            tenant_id=tenant.id,
            parent_id=parent.id if parent else None,
            kind=kind,
            name=name,
            code=code,
            discipline=discipline,
            specialty_id=specialties[specialty_code].id if specialty_code else None,
            capacity=capacity or {},
            depth=(parent.depth + 1) if parent else 0,
        )
        unit.parent = parent
        unit.path = unit.compute_path()
        db.add(unit)
        db.flush()
        units[code] = unit
        return unit

    hospital = add(
        "UTH", "University Teaching Hospital (Demo)", "hospital", None,
        capacity={"beds": 850, "operating_theatres": 12, "icu_beds": 18,
                  "library_seats": 240, "skills_lab_stations": 14, "lecture_theatres": 6},
    )

    clinical = add("FAC-CLIN", "Faculty of Clinical Sciences", "faculty", hospital)
    dental = add("FAC-DENT", "Faculty of Dental Sciences", "faculty", hospital,
                 discipline="dental")

    add("DEPT-SURG", "Department of Surgery", "department", clinical, "SURG",
        {"beds": 120, "operating_theatres": 4, "icu_beds": 6, "clinics_per_week": 8,
         "library_seats": 24, "skills_lab_stations": 4})
    add("DEPT-MED", "Department of Internal Medicine", "department", clinical, "MED",
        {"beds": 160, "icu_beds": 8, "clinics_per_week": 12, "library_seats": 30,
         "skills_lab_stations": 3, "operating_theatres": 0})
    add("DEPT-PAED", "Department of Paediatrics", "department", clinical, "PAED",
        {"beds": 110, "icu_beds": 6, "clinics_per_week": 9, "library_seats": 20,
         "skills_lab_stations": 3, "operating_theatres": 1})
    add("DEPT-OBGYN", "Department of Obstetrics & Gynaecology", "department", clinical, "OBGYN",
        {"beds": 90, "operating_theatres": 3, "labour_ward_beds": 20,
         "clinics_per_week": 10, "library_seats": 18, "skills_lab_stations": 2})
    add("DEPT-ANAES", "Department of Anaesthesia", "department", clinical, "ANAES",
        {"operating_theatres": 12, "icu_beds": 18, "library_seats": 14,
         "skills_lab_stations": 4})
    add("DEPT-RAD", "Department of Radiology", "department", clinical, "RAD",
        {"ct_scanners": 2, "mri_scanners": 1, "ultrasound_units": 6, "library_seats": 12,
         "operating_theatres": 1, "icu_beds": 0, "skills_lab_stations": 2})
    add("DEPT-PATH", "Department of Pathology", "department", clinical, "PATH",
        {"laboratories": 5, "library_seats": 16, "skills_lab_stations": 2})
    add("DEPT-PSYCH", "Department of Psychiatry", "department", clinical, "PSYCH",
        {"beds": 40, "clinics_per_week": 6, "library_seats": 10})
    add("DEPT-COMM", "Department of Community Medicine", "department", clinical, "COMM",
        {"field_sites": 4, "library_seats": 14})
    add("DEPT-ORALSURG", "Department of Oral & Maxillofacial Surgery", "department", dental,
        "DENT-ORAL", {"dental_chairs": 12, "operating_theatres": 1, "library_seats": 10,
                      "skills_lab_stations": 4}, discipline="dental")

    surgery = units["DEPT-SURG"]
    add("UNIT-SURG-GEN", "General Surgery Unit", "unit", surgery, "SURG-GEN",
        {"beds": 48, "operating_theatres": 2})
    add("UNIT-SURG-ORTHO", "Orthopaedic Surgery Unit", "unit", surgery, "SURG-ORTHO",
        {"beds": 36, "operating_theatres": 1})
    add("UNIT-SURG-URO", "Urology Unit", "unit", surgery, "SURG-URO",
        {"beds": 20, "operating_theatres": 1})
    add("UNIT-SURG-PAED", "Paediatric Surgery Unit", "unit", surgery, "SURG-PAED",
        {"beds": 16, "operating_theatres": 1})

    medicine = units["DEPT-MED"]
    add("UNIT-MED-CARD", "Cardiology Unit", "unit", medicine, "MED-CARD", {"beds": 30})
    add("UNIT-MED-NEPH", "Nephrology Unit", "unit", medicine, "MED-NEPH",
        {"beds": 24, "dialysis_stations": 8})
    add("UNIT-MED-NEURO", "Neurology Unit", "unit", medicine, "MED-NEURO", {"beds": 26})
    add("UNIT-MED-ID", "Infectious Diseases Unit", "unit", medicine, "MED-ID", {"beds": 20})

    log(f"organisational units: {len(units)} across {len({u.kind for u in units.values()})} levels")
    return units


# --------------------------------------------------------------------------
PEOPLE = {
    "leadership": [
        ("cmd", "Prof.", "Adaeze", "Nwachukwu", "chief_medical_director", "UTH"),
        ("md", "Prof.", "Ibrahim", "Suleiman", "medical_director", "UTH"),
        ("cmac", "Prof.", "Folasade", "Adeyemi", "cmac", "UTH"),
        ("drt", "Prof.", "Chukwuemeka", "Okoro", "director_residency", "UTH"),
        ("deputy.drt", "Dr.", "Ngozi", "Balogun", "deputy_director_residency", "UTH"),
        ("dean", "Prof.", "Yusuf", "Danjuma", "dean", "FAC-CLIN"),
        ("qa", "Dr.", "Halima", "Bello", "quality_assurance", "UTH"),
    ],
    "departments": {
        "DEPT-SURG": [
            ("hod.surgery", "Prof.", "Olusegun", "Ajayi", "head_of_department"),
            ("coord.surgery", "Dr.", "Amina", "Yakubu", "residency_coordinator"),
            ("consultant1", "Dr.", "Tunde", "Fagbemi", "consultant"),
            ("consultant2", "Dr.", "Chiamaka", "Eze", "consultant"),
            ("consultant3", "Dr.", "Samuel", "Oyelaran", "consultant"),
        ],
        "DEPT-MED": [
            ("hod.medicine", "Prof.", "Grace", "Umeh", "head_of_department"),
            ("coord.medicine", "Dr.", "Bashir", "Lawal", "residency_coordinator"),
            ("consultant4", "Dr.", "Kemi", "Adebayo", "consultant"),
            ("consultant5", "Dr.", "Emeka", "Nnaji", "consultant"),
        ],
        "DEPT-PAED": [
            ("hod.paeds", "Prof.", "Zainab", "Musa", "head_of_department"),
            ("consultant6", "Dr.", "Peter", "Achebe", "consultant"),
        ],
        "DEPT-OBGYN": [
            ("hod.obgyn", "Prof.", "Bolanle", "Ogundipe", "head_of_department"),
            ("consultant7", "Dr.", "Fatima", "Garba", "consultant"),
        ],
    },
}

SURGERY_TRAINEES = [
    ("snr.registrar1", "Dr.", "Uche", "Nwosu", "senior_registrar", 4),
    ("snr.registrar2", "Dr.", "Aisha", "Mohammed", "senior_registrar", 4),
    ("registrar1", "Dr.", "Tobi", "Adewale", "registrar", 2),
    ("registrar2", "Dr.", "Chinelo", "Okafor", "registrar", 2),
    ("registrar3", "Dr.", "Musa", "Abdullahi", "registrar", 3),
    ("registrar4", "Dr.", "Blessing", "Etim", "registrar", 1),
]

MEDICINE_TRAINEES = [
    ("snr.registrar3", "Dr.", "Damilola", "Ojo", "senior_registrar", 4),
    ("registrar5", "Dr.", "Hauwa", "Ibrahim", "registrar", 2),
    ("registrar6", "Dr.", "Kelechi", "Obi", "registrar", 3),
]

HOUSE_OFFICERS = [
    ("houseofficer1", "Dr.", "Seyi", "Alabi", "house_officer", 1),
    ("houseofficer2", "Dr.", "Rukayat", "Salami", "house_officer", 1),
    ("houseofficer3", "Dr.", "Ifeanyi", "Duru", "house_officer", 1),
]


def make_user(db: Session, tenant: Tenant, *, local: str, title: str, first: str,
              last: str, discipline: str = "medical", is_platform_admin: bool = False,
              registration_prefix: str = "MDCN") -> User:
    email = f"{local}@uthdemo.health" if not is_platform_admin else f"{local}@rtc.health"
    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        return existing

    user = User(
        tenant_id=None if is_platform_admin else tenant.id,
        email=email,
        first_name=first,
        last_name=last,
        title=title,
        phone=f"+234 80{rng.randint(10000000, 99999999)}",
        discipline=discipline,
        registration_number=f"{registration_prefix}/{rng.randint(40000, 99999)}",
        staff_number=f"UTH{rng.randint(1000, 9999)}",
        hashed_password=hash_password(DEMO_PASSWORD),
        status=UserStatus.ACTIVE,
        is_platform_admin=is_platform_admin,
        email_verified_at=utcnow(),
        password_changed_at=utcnow(),
        qualifications=[
            {"award": "MBBS", "institution": "University of Lagos",
             "year": rng.randint(2005, 2019)}
        ],
        preferences={"theme": "system", "locale": "en-NG"},
    )
    db.add(user)
    db.flush()
    return user


def grant(db: Session, user: User, role: Role, org_unit: OrgUnit | None, *, primary: bool = True) -> None:
    existing = db.execute(
        select(RoleAssignment).where(
            RoleAssignment.user_id == user.id,
            RoleAssignment.role_id == role.id,
            RoleAssignment.org_unit_id == (org_unit.id if org_unit else None),
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            RoleAssignment(
                user_id=user.id, role_id=role.id,
                org_unit_id=org_unit.id if org_unit else None, is_primary=primary,
            )
        )


def build_people(db: Session, tenant: Tenant, units: dict[str, OrgUnit],
                 roles: dict[str, Role]) -> dict[str, User]:
    people: dict[str, User] = {}

    superadmin = make_user(db, tenant, local="super", title="", first="Platform",
                           last="Administrator", is_platform_admin=True)
    grant(db, superadmin, roles["national_super_admin"], None)
    people["super"] = superadmin

    for local, title, first, last, role_code, unit_code in PEOPLE["leadership"]:
        user = make_user(db, tenant, local=local, title=title, first=first, last=last)
        grant(db, user, roles[role_code], units[unit_code])
        people[local] = user

    for unit_code, members in PEOPLE["departments"].items():
        for local, title, first, last, role_code in members:
            discipline = "dental" if unit_code == "DEPT-ORALSURG" else "medical"
            user = make_user(db, tenant, local=local, title=title, first=first, last=last,
                             discipline=discipline)
            grant(db, user, roles[role_code], units[unit_code])
            people[local] = user

            if role_code in SUPERVISOR_ROLE_CODES and db.execute(
                select(SupervisorProfile).where(SupervisorProfile.user_id == user.id)
            ).scalar_one_or_none() is None:
                db.add(
                    SupervisorProfile(
                        user_id=user.id,
                        tenant_id=tenant.id,
                        expertise=rng.sample(
                            ["laparoscopy", "trauma", "oncology", "paediatric surgery",
                             "endocrine", "hepatobiliary", "cardiology", "nephrology",
                             "infectious diseases", "medical education", "health systems"],
                            k=3,
                        ),
                        methodologies=rng.sample(
                            ["randomised controlled trial", "cohort study", "qualitative research",
                             "biostatistics", "systematic review", "health economics"],
                            k=2,
                        ),
                        max_supervisees=rng.randint(3, 6),
                        max_clinical_trainees=rng.randint(5, 9),
                        completed_supervisions=rng.randint(0, 8),
                    )
                )

    for cohort, unit_code in (
        (SURGERY_TRAINEES, "DEPT-SURG"),
        (MEDICINE_TRAINEES, "DEPT-MED"),
        (HOUSE_OFFICERS, "DEPT-SURG"),
    ):
        for local, title, first, last, role_code, _year in cohort:
            user = make_user(db, tenant, local=local, title=title, first=first, last=last)
            grant(db, user, roles[role_code], units[unit_code])
            people[local] = user

    db.flush()
    log(f"people: {len(people)} accounts with scoped role assignments")
    return people


# ==========================================================================
# Curricula
# ==========================================================================
SURGERY_COMPETENCIES = [
    ("SURG-EPA-01", "Assess and manage the acute surgical abdomen",
     CompetencyDomain.PATIENT_CARE, True, {"1": "2_direct_supervision", "2": "3_indirect_supervision",
                                            "3": "4_independent", "4": "5_supervise_others"}),
    ("SURG-EPA-02", "Perform an appendicectomy",
     CompetencyDomain.PROCEDURAL_SKILL, True, {"1": "1_observe_only", "2": "2_direct_supervision",
                                                "3": "3_indirect_supervision", "4": "4_independent"}),
    ("SURG-EPA-03", "Perform an open inguinal hernia repair",
     CompetencyDomain.PROCEDURAL_SKILL, True, {"1": "1_observe_only", "2": "2_direct_supervision",
                                                "3": "3_indirect_supervision", "4": "4_independent"}),
    ("SURG-EPA-04", "Manage the surgical patient perioperatively",
     CompetencyDomain.PATIENT_CARE, True, {"1": "2_direct_supervision", "2": "3_indirect_supervision",
                                            "3": "4_independent", "4": "5_supervise_others"}),
    ("SURG-EPA-05", "Lead a surgical ward round",
     CompetencyDomain.LEADERSHIP, True, {"2": "2_direct_supervision", "3": "3_indirect_supervision",
                                          "4": "4_independent"}),
    ("SURG-C-06", "Interpret surgical imaging",
     CompetencyDomain.MEDICAL_KNOWLEDGE, False, {"1": "2_direct_supervision", "3": "4_independent"}),
    ("SURG-C-07", "Obtain valid informed consent",
     CompetencyDomain.COMMUNICATION, False, {"1": "3_indirect_supervision", "2": "4_independent"}),
    ("SURG-C-08", "Break bad news compassionately",
     CompetencyDomain.COMMUNICATION, False, {"2": "3_indirect_supervision", "3": "4_independent"}),
    ("SURG-C-09", "Practise within ethical and professional standards",
     CompetencyDomain.PROFESSIONALISM, False, {"1": "4_independent"}),
    ("SURG-C-10", "Teach medical students and junior colleagues",
     CompetencyDomain.TEACHING, False, {"2": "3_indirect_supervision", "4": "4_independent"}),
    ("SURG-C-11", "Design and conduct a research project",
     CompetencyDomain.RESEARCH, False, {"3": "3_indirect_supervision", "4": "4_independent"}),
    ("SURG-C-12", "Audit and improve surgical practice",
     CompetencyDomain.PRACTICE_BASED_LEARNING, False, {"3": "3_indirect_supervision",
                                                        "4": "4_independent"}),
]

MEDICINE_COMPETENCIES = [
    ("MED-EPA-01", "Assess and manage the acutely unwell medical patient",
     CompetencyDomain.PATIENT_CARE, True, {"1": "2_direct_supervision", "2": "3_indirect_supervision",
                                            "3": "4_independent", "4": "5_supervise_others"}),
    ("MED-EPA-02", "Perform a diagnostic lumbar puncture",
     CompetencyDomain.PROCEDURAL_SKILL, True, {"1": "2_direct_supervision", "2": "3_indirect_supervision",
                                                "3": "4_independent"}),
    ("MED-EPA-03", "Manage a medical outpatient clinic",
     CompetencyDomain.PATIENT_CARE, True, {"2": "2_direct_supervision", "3": "3_indirect_supervision",
                                            "4": "4_independent"}),
    ("MED-C-04", "Interpret electrocardiograms",
     CompetencyDomain.MEDICAL_KNOWLEDGE, False, {"1": "3_indirect_supervision", "2": "4_independent"}),
    ("MED-C-05", "Prescribe safely and rationally",
     CompetencyDomain.SYSTEMS_BASED_PRACTICE, False, {"1": "3_indirect_supervision",
                                                       "2": "4_independent"}),
    ("MED-C-06", "Communicate a management plan to patients and families",
     CompetencyDomain.COMMUNICATION, False, {"1": "3_indirect_supervision", "2": "4_independent"}),
    ("MED-C-07", "Demonstrate professionalism and reliability",
     CompetencyDomain.PROFESSIONALISM, False, {"1": "4_independent"}),
    ("MED-C-08", "Present at a journal club",
     CompetencyDomain.TEACHING, False, {"2": "3_indirect_supervision", "3": "4_independent"}),
    ("MED-C-09", "Complete a dissertation to College standard",
     CompetencyDomain.RESEARCH, False, {"3": "3_indirect_supervision", "4": "4_independent"}),
]


def build_surgery_curriculum(db: Session, tenant: Tenant, units: dict[str, OrgUnit],
                             specialties: dict[str, Specialty]) -> tuple[Programme, CurriculumVersion]:
    programme = db.execute(
        select(Programme).where(Programme.tenant_id == tenant.id, Programme.code == "SURG-RES")
    ).scalar_one_or_none()
    if programme is not None:
        return programme, programme.versions[0]

    programme = Programme(
        tenant_id=tenant.id,
        org_unit_id=units["DEPT-SURG"].id,
        specialty_id=specialties["SURG-GEN"].id,
        code="SURG-RES",
        name="General Surgery Residency",
        programme_type=ProgrammeType.RESIDENCY_JUNIOR,
        entry_level=TrainingLevel.REGISTRAR,
        exit_level=TrainingLevel.SENIOR_REGISTRAR,
        awarding_body="wacs",
        awarding_body_name="West African College of Surgeons",
        duration_months=48,
        annual_intake=6,
        description=(
            "Four-year general surgery residency leading to the Part II fellowship "
            "examination, delivered against WACS and NPMCN requirements."
        ),
    )
    db.add(programme)
    db.flush()

    version = CurriculumVersion(
        tenant_id=tenant.id,
        programme_id=programme.id,
        version="2026.1",
        title="General Surgery Residency Curriculum 2026",
        status=CurriculumStatus.ACTIVE,
        effective_from=date(2026, 1, 1),
        aims=(
            "To produce surgeons who are technically competent, clinically sound, "
            "research-literate and capable of leading a surgical service."
        ),
        score_weights={
            ScoreDomain.CLINICAL_COMPETENCY: 0.30,
            ScoreDomain.ACADEMIC: 0.13,
            ScoreDomain.ATTENDANCE: 0.12,
            ScoreDomain.RESEARCH: 0.15,
            ScoreDomain.PROFESSIONALISM: 0.12,
            ScoreDomain.TEACHING: 0.07,
            ScoreDomain.LEADERSHIP: 0.04,
            ScoreDomain.EXAM_READINESS: 0.07,
        },
    )
    db.add(version)
    db.flush()

    years: dict[int, TrainingYear] = {}
    for sequence in (1, 2, 3, 4):
        level = TrainingLevel.REGISTRAR if sequence <= 2 else TrainingLevel.SENIOR_REGISTRAR
        year = TrainingYear(
            tenant_id=tenant.id,
            curriculum_version_id=version.id,
            sequence=sequence,
            name=f"Year {sequence}",
            level=level,
            duration_months=12,
            objectives=[
                "Consolidate assessment and resuscitation of the surgical patient"
                if sequence == 1 else "Develop independent operative capability",
                "Contribute to departmental academic activity",
                "Progress the research portfolio",
            ],
            expectations={
                "duties": "Ward cover, theatre lists, emergency calls and clinics as rostered.",
                "professionalism": "Punctuality, complete records, respectful communication.",
                "leadership": "Supervise house officers; lead ward rounds from year 3.",
                "teaching": "Teach medical students weekly.",
                "research": "Registered dissertation by end of year 2; data collection by year 3.",
            },
        )
        db.add(year)
        db.flush()
        years[sequence] = year

        rotations = [
            ("General Surgery", "UNIT-SURG-GEN", 24, False),
            ("Orthopaedic Surgery", "UNIT-SURG-ORTHO", 12, False),
            ("Urology", "UNIT-SURG-URO", 8, False),
            ("Paediatric Surgery", "UNIT-SURG-PAED", 8, sequence >= 3),
        ]
        for index, (name, unit_code, weeks, elective) in enumerate(rotations, start=1):
            db.add(
                RotationTemplate(
                    tenant_id=tenant.id,
                    training_year_id=year.id,
                    org_unit_id=units[unit_code].id,
                    name=name,
                    code=f"{unit_code}-Y{sequence}",
                    sequence=index,
                    duration_weeks=weeks,
                    is_elective=elective,
                    is_mandatory=not elective,
                    max_trainees=4,
                    objectives=[
                        f"Manage the common {name.lower()} presentations",
                        "Achieve the procedural minima for this posting",
                        "Complete the required workplace-based assessments",
                    ],
                    required_assessments=["MINI-CEX", "DOPS", "CBD"],
                )
            )

    competencies: dict[str, Competency] = {}
    for order, (code, title, domain, is_epa, targets) in enumerate(SURGERY_COMPETENCIES):
        competency = Competency(
            tenant_id=tenant.id,
            curriculum_version_id=version.id,
            code=code,
            title=title,
            domain=domain,
            is_epa=is_epa,
            target_by_year=targets,
            exit_target=EntrustmentLevel.INDEPENDENT,
            assessment_methods=["mini_cex", "dops", "cbd"] if is_epa else ["cbd", "msf"],
            sort_order=order,
        )
        db.add(competency)
        db.flush()
        competencies[code] = competency

    _surgery_requirements(db, tenant, version, years, competencies)
    db.flush()
    log(f"curriculum: {programme.name} v{version.version} "
        f"({len(years)} years, {len(competencies)} competencies)")
    return programme, version


def _surgery_requirements(db: Session, tenant: Tenant, version: CurriculumVersion,
                          years: dict[int, TrainingYear], competencies: dict[str, Competency]) -> None:
    """The policy layer, expressed entirely as data."""

    def rule(**kwargs) -> None:
        db.add(RequirementRule(tenant_id=tenant.id, curriculum_version_id=version.id, **kwargs))

    # -- procedural minima, by year ---------------------------------------
    minima = {1: 40, 2: 80, 3: 120, 4: 160}
    for year_seq, target in minima.items():
        rule(
            code=f"SURG-PROC-Y{year_seq}",
            label=f"Year {year_seq}: {target} major procedures assisted or performed",
            kind=RequirementKind.PROCEDURE_ROLE_COUNT,
            operator="gte",
            target_value=target,
            parameters={
                "roles": ["assisted", "performed_supervised", "performed_independent"],
                "grade": "major",
                "training_year": year_seq,
            },
            scope=RequirementScope.TRAINING_YEAR,
            severity=RequirementSeverity.MANDATORY,
            training_year_id=years[year_seq].id,
            score_domain=ScoreDomain.CLINICAL_COMPETENCY,
            weight=2.0,
            guidance="Only entries validated by a consultant are counted.",
            source_reference="WACS Faculty of Surgery — operative experience guidance",
        )

    rule(
        code="SURG-PROC-INDEP",
        label="60 major procedures performed independently before Part II",
        kind=RequirementKind.PROCEDURE_ROLE_COUNT,
        operator="gte",
        target_value=60,
        parameters={"role": "performed_independent", "grade": "major"},
        scope=RequirementScope.EXAM_ELIGIBILITY,
        severity=RequirementSeverity.MANDATORY,
        score_domain=ScoreDomain.CLINICAL_COMPETENCY,
        weight=2.5,
        source_reference="WACS Part II eligibility",
    )
    rule(
        code="SURG-CLINIC",
        label="150 outpatient clinic sessions across the programme",
        kind=RequirementKind.CLINIC_COUNT,
        operator="gte",
        target_value=150,
        scope=RequirementScope.PROGRAMME,
        severity=RequirementSeverity.MANDATORY,
        score_domain=ScoreDomain.CLINICAL_COMPETENCY,
    )
    rule(
        code="SURG-EMERG",
        label="200 emergency calls attended",
        kind=RequirementKind.LOGBOOK_ENTRY_COUNT,
        operator="gte",
        target_value=200,
        parameters={"entry_types": ["emergency_call"]},
        scope=RequirementScope.PROGRAMME,
        severity=RequirementSeverity.RECOMMENDED,
        score_domain=ScoreDomain.CLINICAL_COMPETENCY,
    )

    # -- competency / EPA attainment --------------------------------------
    rule(
        code="SURG-EPA-ALL",
        label="All EPAs at indirect supervision or better",
        kind=RequirementKind.EPA_LEVEL,
        operator="gte",
        target_value=3,
        parameters={"epas_only": True, "aggregate": "min", "level": "3_indirect_supervision"},
        scope=RequirementScope.PROMOTION,
        severity=RequirementSeverity.MANDATORY,
        score_domain=ScoreDomain.CLINICAL_COMPETENCY,
        weight=3.0,
        guidance="Every Entrustable Professional Activity must reach the target level; "
                 "the lowest-rated EPA determines the result.",
    )
    rule(
        code="SURG-EPA-EXIT",
        label="80% of EPAs at independent practice for exit",
        kind=RequirementKind.EPA_LEVEL,
        operator="gte",
        target_value=80,
        parameters={"epas_only": True, "aggregate": "percent_at_target", "level_value": 4},
        scope=RequirementScope.EXAM_ELIGIBILITY,
        severity=RequirementSeverity.MANDATORY,
        score_domain=ScoreDomain.CLINICAL_COMPETENCY,
        weight=2.0,
    )

    # -- academic ----------------------------------------------------------
    rule(
        code="SURG-ATT-GR",
        label="75% attendance at departmental academic meetings",
        kind=RequirementKind.ACADEMIC_ATTENDANCE_PCT,
        operator="gte",
        target_value=75,
        parameters={"activity_kinds": ["grand_round", "seminar", "journal_club",
                                        "mortality_meeting", "morbidity_meeting"]},
        scope=RequirementScope.PROGRAMME,
        severity=RequirementSeverity.MANDATORY,
        score_domain=ScoreDomain.ATTENDANCE,
        weight=2.0,
        source_reference="NPMCN residency training guidelines",
    )
    rule(
        code="SURG-PRESENT",
        label="8 academic presentations across the programme",
        kind=RequirementKind.ACTIVITY_PRESENTATION_COUNT,
        operator="gte",
        target_value=8,
        parameters={"include_conferences": True},
        scope=RequirementScope.PROGRAMME,
        severity=RequirementSeverity.MANDATORY,
        score_domain=ScoreDomain.ACADEMIC,
        weight=1.5,
    )
    rule(
        code="SURG-MM",
        label="Attend 80% of mortality and morbidity meetings",
        kind=RequirementKind.ACADEMIC_ATTENDANCE_PCT,
        operator="gte",
        target_value=80,
        parameters={"activity_kinds": ["mortality_meeting", "morbidity_meeting"]},
        scope=RequirementScope.PROGRAMME,
        severity=RequirementSeverity.RECOMMENDED,
        score_domain=ScoreDomain.ACADEMIC,
    )

    # -- assessment --------------------------------------------------------
    rule(
        code="SURG-WPBA",
        label="12 workplace-based assessments passed per year",
        kind=RequirementKind.ASSESSMENT_PASS_COUNT,
        operator="gte",
        target_value=12,
        parameters={"assessment_kinds": ["mini_cex", "dops", "cbd"]},
        scope=RequirementScope.TRAINING_YEAR,
        severity=RequirementSeverity.MANDATORY,
        score_domain=ScoreDomain.CLINICAL_COMPETENCY,
        weight=2.0,
    )
    rule(
        code="SURG-WPBA-MEAN",
        label="Mean workplace assessment score at least 65%",
        kind=RequirementKind.ASSESSMENT_MEAN_SCORE,
        operator="gte",
        target_value=65,
        parameters={"assessment_kinds": ["mini_cex", "dops", "cbd"]},
        scope=RequirementScope.PROGRAMME,
        severity=RequirementSeverity.RECOMMENDED,
        score_domain=ScoreDomain.CLINICAL_COMPETENCY,
    )

    # -- research ----------------------------------------------------------
    rule(
        code="SURG-DISS",
        label="Dissertation at data collection stage or beyond by year 3",
        kind=RequirementKind.DISSERTATION_STAGE,
        operator="gte",
        target_value=0,
        parameters={"stage": "data_collection", "training_year": [3, 4]},
        scope=RequirementScope.PROMOTION,
        severity=RequirementSeverity.MANDATORY,
        score_domain=ScoreDomain.RESEARCH,
        weight=2.5,
        source_reference="NPMCN dissertation regulations",
    )
    rule(
        code="SURG-DISS-EXIT",
        label="Dissertation submitted to the College before Part II",
        kind=RequirementKind.DISSERTATION_STAGE,
        operator="gte",
        target_value=0,
        parameters={"stage": "college_submission"},
        scope=RequirementScope.EXAM_ELIGIBILITY,
        severity=RequirementSeverity.MANDATORY,
        score_domain=ScoreDomain.RESEARCH,
        weight=3.0,
    )
    rule(
        code="SURG-PUB",
        label="1 peer-reviewed publication",
        kind=RequirementKind.PUBLICATION_COUNT,
        operator="gte",
        target_value=1,
        parameters={"peer_reviewed_only": True, "verified_only": True},
        scope=RequirementScope.PROGRAMME,
        severity=RequirementSeverity.RECOMMENDED,
        score_domain=ScoreDomain.RESEARCH,
        weight=1.5,
    )

    # -- leadership --------------------------------------------------------
    rule(
        code="SURG-LEAD-ROUND",
        label="Lead ward rounds at indirect supervision or better",
        kind=RequirementKind.COMPETENCY_LEVEL,
        operator="gte",
        target_value=3,
        parameters={"competency_codes": ["SURG-EPA-05"], "aggregate": "min",
                    "level": "3_indirect_supervision"},
        scope=RequirementScope.PROMOTION,
        severity=RequirementSeverity.RECOMMENDED,
        score_domain=ScoreDomain.LEADERSHIP,
        competency_id=competencies["SURG-EPA-05"].id,
        weight=1.5,
        guidance="Assessed by the consultant supervising the round.",
    )
    rule(
        code="SURG-LEAD-SUPERVISE",
        label="20 procedures supervising a more junior colleague",
        kind=RequirementKind.PROCEDURE_ROLE_COUNT,
        operator="gte",
        target_value=20,
        parameters={"role": "supervised_other", "training_year": [3, 4]},
        scope=RequirementScope.PROGRAMME,
        severity=RequirementSeverity.RECOMMENDED,
        score_domain=ScoreDomain.LEADERSHIP,
        guidance="Demonstrates readiness to train others, expected of senior registrars.",
    )

    # -- teaching, CME, duty ----------------------------------------------
    rule(
        code="SURG-TEACH",
        label="40 hours of documented teaching",
        kind=RequirementKind.TEACHING_HOURS,
        operator="gte",
        target_value=40,
        scope=RequirementScope.PROGRAMME,
        severity=RequirementSeverity.RECOMMENDED,
        score_domain=ScoreDomain.TEACHING,
    )
    rule(
        code="SURG-CME",
        label="30 CME credits per training year",
        kind=RequirementKind.CME_CREDITS,
        operator="gte",
        target_value=30,
        scope=RequirementScope.TRAINING_YEAR,
        severity=RequirementSeverity.RECOMMENDED,
        score_domain=ScoreDomain.ACADEMIC,
    )
    rule(
        code="SURG-DUTY",
        label="90% duty attendance",
        kind=RequirementKind.DUTY_ATTENDANCE_PCT,
        operator="gte",
        target_value=90,
        scope=RequirementScope.PROGRAMME,
        severity=RequirementSeverity.MANDATORY,
        score_domain=ScoreDomain.ATTENDANCE,
        weight=1.5,
    )
    rule(
        code="SURG-ROT",
        label="All rotations for the year completed",
        kind=RequirementKind.ROTATION_COMPLETION,
        operator="gte",
        target_value=100,
        scope=RequirementScope.PROMOTION,
        severity=RequirementSeverity.MANDATORY,
        score_domain=ScoreDomain.CLINICAL_COMPETENCY,
        weight=2.0,
    )
    # A composite metric showing that institutions can express derived policy without
    # any new measurement code.
    rule(
        code="SURG-OP-INDEP-RATIO",
        label="At least 25% of major cases performed independently",
        kind=RequirementKind.CUSTOM_EXPRESSION,
        operator="gte",
        target_value=25,
        parameters={
            "expression": "independent / total * 100",
            "inputs": {
                "independent": {
                    "kind": RequirementKind.PROCEDURE_ROLE_COUNT,
                    "parameters": {"role": "performed_independent", "grade": "major"},
                },
                "total": {
                    "kind": RequirementKind.PROCEDURE_COUNT,
                    "parameters": {"grade": "major"},
                },
            },
        },
        scope=RequirementScope.PROGRAMME,
        severity=RequirementSeverity.RECOMMENDED,
        score_domain=ScoreDomain.CLINICAL_COMPETENCY,
        guidance="Measures operative independence rather than raw exposure.",
    )


def build_medicine_curriculum(db: Session, tenant: Tenant, units: dict[str, OrgUnit],
                              specialties: dict[str, Specialty]) -> tuple[Programme, CurriculumVersion]:
    programme = db.execute(
        select(Programme).where(Programme.tenant_id == tenant.id, Programme.code == "MED-RES")
    ).scalar_one_or_none()
    if programme is not None:
        return programme, programme.versions[0]

    programme = Programme(
        tenant_id=tenant.id,
        org_unit_id=units["DEPT-MED"].id,
        specialty_id=specialties["MED"].id,
        code="MED-RES",
        name="Internal Medicine Residency",
        programme_type=ProgrammeType.RESIDENCY_JUNIOR,
        entry_level=TrainingLevel.REGISTRAR,
        exit_level=TrainingLevel.SENIOR_REGISTRAR,
        awarding_body="wacp",
        awarding_body_name="West African College of Physicians",
        duration_months=48,
        annual_intake=8,
    )
    db.add(programme)
    db.flush()

    version = CurriculumVersion(
        tenant_id=tenant.id,
        programme_id=programme.id,
        version="2026.1",
        title="Internal Medicine Residency Curriculum 2026",
        status=CurriculumStatus.ACTIVE,
        effective_from=date(2026, 1, 1),
        aims="To produce physicians competent in the breadth of internal medicine.",
    )
    db.add(version)
    db.flush()

    unit_codes = ["UNIT-MED-CARD", "UNIT-MED-NEPH", "UNIT-MED-NEURO", "UNIT-MED-ID"]
    years = {}
    for sequence in (1, 2, 3, 4):
        year = TrainingYear(
            tenant_id=tenant.id, curriculum_version_id=version.id, sequence=sequence,
            name=f"Year {sequence}",
            level=TrainingLevel.REGISTRAR if sequence <= 2 else TrainingLevel.SENIOR_REGISTRAR,
            duration_months=12,
            objectives=["Manage the acute medical take", "Develop subspecialty exposure"],
        )
        db.add(year)
        db.flush()
        years[sequence] = year
        for index, code in enumerate(unit_codes, start=1):
            db.add(
                RotationTemplate(
                    tenant_id=tenant.id, training_year_id=year.id,
                    org_unit_id=units[code].id,
                    name=units[code].name.replace(" Unit", ""),
                    code=f"{code}-Y{sequence}", sequence=index, duration_weeks=13,
                    max_trainees=3,
                    objectives=["Manage the common presentations of this subspecialty"],
                    required_assessments=["MINI-CEX", "CBD"],
                )
            )

    competencies = {}
    for order, (code, title, domain, is_epa, targets) in enumerate(MEDICINE_COMPETENCIES):
        competency = Competency(
            tenant_id=tenant.id, curriculum_version_id=version.id, code=code, title=title,
            domain=domain, is_epa=is_epa, target_by_year=targets,
            exit_target=EntrustmentLevel.INDEPENDENT, sort_order=order,
        )
        db.add(competency)
        db.flush()
        competencies[code] = competency

    def rule(**kwargs) -> None:
        db.add(RequirementRule(tenant_id=tenant.id, curriculum_version_id=version.id, **kwargs))

    rule(code="MED-ADMIT", label="300 admissions clerked per year",
         kind=RequirementKind.LOGBOOK_ENTRY_COUNT, operator="gte", target_value=300,
         parameters={"entry_types": ["admission"]}, scope=RequirementScope.TRAINING_YEAR,
         severity=RequirementSeverity.MANDATORY, score_domain=ScoreDomain.CLINICAL_COMPETENCY,
         weight=2.0)
    rule(code="MED-PROC", label="30 diagnostic procedures performed",
         kind=RequirementKind.PROCEDURE_ROLE_COUNT, operator="gte", target_value=30,
         parameters={"roles": ["performed_supervised", "performed_independent"]},
         scope=RequirementScope.PROGRAMME, severity=RequirementSeverity.MANDATORY,
         score_domain=ScoreDomain.CLINICAL_COMPETENCY)
    rule(code="MED-CLINIC", label="200 clinic sessions", kind=RequirementKind.CLINIC_COUNT,
         operator="gte", target_value=200, scope=RequirementScope.PROGRAMME,
         severity=RequirementSeverity.MANDATORY, score_domain=ScoreDomain.CLINICAL_COMPETENCY)
    rule(code="MED-JC", label="75% attendance at journal club and grand rounds",
         kind=RequirementKind.ACADEMIC_ATTENDANCE_PCT, operator="gte", target_value=75,
         parameters={"activity_kinds": ["journal_club", "grand_round", "seminar"]},
         scope=RequirementScope.PROGRAMME, severity=RequirementSeverity.MANDATORY,
         score_domain=ScoreDomain.ATTENDANCE, weight=2.0)
    rule(code="MED-EPA", label="All EPAs at indirect supervision or better",
         kind=RequirementKind.EPA_LEVEL, operator="gte", target_value=3,
         parameters={"epas_only": True, "aggregate": "min", "level": "3_indirect_supervision"},
         scope=RequirementScope.PROMOTION, severity=RequirementSeverity.MANDATORY,
         score_domain=ScoreDomain.CLINICAL_COMPETENCY, weight=3.0)
    rule(code="MED-DISS", label="Dissertation registered and past ethics approval",
         kind=RequirementKind.DISSERTATION_STAGE, operator="gte", target_value=0,
         parameters={"stage": "ethics_approval"}, scope=RequirementScope.PROMOTION,
         severity=RequirementSeverity.MANDATORY, score_domain=ScoreDomain.RESEARCH, weight=2.0)
    rule(code="MED-WPBA", label="10 workplace-based assessments passed per year",
         kind=RequirementKind.ASSESSMENT_PASS_COUNT, operator="gte", target_value=10,
         parameters={"assessment_kinds": ["mini_cex", "cbd"]},
         scope=RequirementScope.TRAINING_YEAR, severity=RequirementSeverity.MANDATORY,
         score_domain=ScoreDomain.CLINICAL_COMPETENCY, weight=2.0)
    rule(code="MED-ROT", label="All rotations for the year completed",
         kind=RequirementKind.ROTATION_COMPLETION, operator="gte", target_value=100,
         scope=RequirementScope.PROMOTION, severity=RequirementSeverity.MANDATORY,
         score_domain=ScoreDomain.CLINICAL_COMPETENCY, weight=2.0)

    db.flush()
    log(f"curriculum: {programme.name} v{version.version}")
    return programme, version


def build_housemanship(db: Session, tenant: Tenant, units: dict[str, OrgUnit]) -> tuple[Programme, CurriculumVersion]:
    programme = db.execute(
        select(Programme).where(Programme.tenant_id == tenant.id, Programme.code == "HOUSE")
    ).scalar_one_or_none()
    if programme is not None:
        return programme, programme.versions[0]

    programme = Programme(
        tenant_id=tenant.id,
        org_unit_id=units["UTH"].id,
        code="HOUSE",
        name="Housemanship (Internship) Programme",
        programme_type=ProgrammeType.HOUSEMANSHIP,
        entry_level=TrainingLevel.HOUSE_OFFICER,
        exit_level=TrainingLevel.MEDICAL_OFFICER,
        awarding_body="mdcn",
        awarding_body_name="Medical & Dental Council of Nigeria",
        duration_months=12,
        annual_intake=40,
        description="Twelve-month MDCN housemanship across the six required postings.",
    )
    db.add(programme)
    db.flush()

    version = CurriculumVersion(
        tenant_id=tenant.id, programme_id=programme.id, version="2026.1",
        title="MDCN Housemanship Curriculum 2026", status=CurriculumStatus.ACTIVE,
        effective_from=date(2026, 1, 1),
        aims="To produce safe, independent medical officers fit for full registration.",
    )
    db.add(version)
    db.flush()

    year = TrainingYear(
        tenant_id=tenant.id, curriculum_version_id=version.id, sequence=1,
        name="Housemanship Year", level=TrainingLevel.HOUSE_OFFICER, duration_months=12,
        objectives=["Practise safely under supervision across the core disciplines"],
    )
    db.add(year)
    db.flush()

    postings = [
        ("Internal Medicine", "DEPT-MED", 12),
        ("Surgery", "DEPT-SURG", 12),
        ("Paediatrics", "DEPT-PAED", 10),
        ("Obstetrics & Gynaecology", "DEPT-OBGYN", 10),
        ("Emergency Medicine", "DEPT-MED", 4),
        ("Community Medicine", "DEPT-COMM", 4),
    ]
    for index, (name, unit_code, weeks) in enumerate(postings, start=1):
        db.add(
            RotationTemplate(
                tenant_id=tenant.id, training_year_id=year.id,
                org_unit_id=units[unit_code].id, name=name, code=f"HOUSE-{index}",
                sequence=index, duration_weeks=weeks, max_trainees=12,
                objectives=[f"Meet the MDCN competencies for the {name.lower()} posting"],
                required_assessments=["HO-END"],
            )
        )

    def rule(**kwargs) -> None:
        db.add(RequirementRule(tenant_id=tenant.id, curriculum_version_id=version.id, **kwargs))

    rule(code="HO-ADMIT", label="120 patients clerked",
         kind=RequirementKind.LOGBOOK_ENTRY_COUNT, operator="gte", target_value=120,
         parameters={"entry_types": ["admission"]}, scope=RequirementScope.PROGRAMME,
         severity=RequirementSeverity.MANDATORY, score_domain=ScoreDomain.CLINICAL_COMPETENCY,
         weight=2.0, source_reference="MDCN housemanship logbook requirements")
    rule(code="HO-PROC", label="40 practical procedures performed under supervision",
         kind=RequirementKind.PROCEDURE_ROLE_COUNT, operator="gte", target_value=40,
         parameters={"roles": ["performed_supervised", "performed_independent"]},
         scope=RequirementScope.PROGRAMME, severity=RequirementSeverity.MANDATORY,
         score_domain=ScoreDomain.CLINICAL_COMPETENCY)
    rule(code="HO-DELIVERY", label="20 deliveries conducted",
         kind=RequirementKind.PROCEDURE_COUNT, operator="gte", target_value=20,
         parameters={"procedure_codes": ["Spontaneous vaginal delivery"]},
         scope=RequirementScope.PROGRAMME, severity=RequirementSeverity.MANDATORY,
         score_domain=ScoreDomain.CLINICAL_COMPETENCY)
    rule(code="HO-ATT", label="80% attendance at clinical teaching",
         kind=RequirementKind.ACADEMIC_ATTENDANCE_PCT, operator="gte", target_value=80,
         parameters={"activity_kinds": ["teaching_round", "grand_round", "morning_review"]},
         scope=RequirementScope.PROGRAMME, severity=RequirementSeverity.MANDATORY,
         score_domain=ScoreDomain.ATTENDANCE, weight=2.0)
    rule(code="HO-ROT", label="All six postings completed",
         kind=RequirementKind.ROTATION_COMPLETION, operator="gte", target_value=100,
         scope=RequirementScope.PROMOTION, severity=RequirementSeverity.MANDATORY,
         score_domain=ScoreDomain.CLINICAL_COMPETENCY, weight=3.0)
    rule(code="HO-ASSESS", label="6 end-of-posting assessments passed",
         kind=RequirementKind.ASSESSMENT_PASS_COUNT, operator="gte", target_value=6,
         parameters={"assessment_kinds": ["rotation_end"]}, scope=RequirementScope.PROGRAMME,
         severity=RequirementSeverity.MANDATORY, score_domain=ScoreDomain.CLINICAL_COMPETENCY,
         weight=2.0)

    db.flush()
    log(f"curriculum: {programme.name} v{version.version}")
    return programme, version


# ==========================================================================
# Assessment instruments
# ==========================================================================
def build_assessment_templates(db: Session, tenant: Tenant) -> dict[str, AssessmentTemplate]:
    templates: dict[str, AssessmentTemplate] = {
        t.code: t
        for t in db.execute(
            select(AssessmentTemplate).where(AssessmentTemplate.tenant_id == tenant.id)
        ).scalars()
    }
    if templates:
        return templates

    def scale(key: str, label: str, weight: float = 1.0) -> dict:
        return {
            "key": key, "label": label, "type": "scale", "min": 1, "max": 9,
            "weight": weight, "required": True,
            "anchors": {"1": "Well below expectation for stage",
                        "5": "Meets expectation for stage",
                        "9": "Outstanding for stage"},
        }

    specs = [
        ("MINI-CEX", "Mini Clinical Evaluation Exercise", AssessmentKind.MINI_CEX,
         [scale("history", "History taking"), scale("examination", "Physical examination"),
          scale("professionalism", "Professionalism"), scale("clinical_judgement",
                                                             "Clinical judgement", 1.5),
          scale("communication", "Communication"), scale("organisation", "Organisation & efficiency"),
          scale("overall", "Overall clinical care", 2.0),
          {"key": "comment", "label": "Narrative feedback", "type": "textarea", "required": True}],
         "Direct observation of a focused clinical encounter, 15-20 minutes, followed by "
         "immediate feedback."),
        ("DOPS", "Direct Observation of Procedural Skills", AssessmentKind.DOPS,
         [scale("indication", "Understanding of indications"),
          scale("consent", "Obtaining informed consent"),
          scale("preparation", "Preparation and asepsis"),
          scale("technical", "Technical ability", 2.0),
          scale("aftercare", "Post-procedure management"),
          scale("overall", "Overall procedural performance", 2.0),
          {"key": "procedure", "label": "Procedure observed", "type": "text", "required": True},
          {"key": "comment", "label": "Narrative feedback", "type": "textarea", "required": True}],
         "Observation of a single procedure from consent through to aftercare."),
        ("CBD", "Case-Based Discussion", AssessmentKind.CBD,
         [scale("record_keeping", "Medical record keeping"),
          scale("investigation", "Investigation and referral"),
          scale("management", "Management plan", 1.5),
          scale("follow_up", "Follow-up and future planning"),
          scale("ethics", "Professionalism and ethics"),
          scale("overall", "Overall clinical judgement", 2.0),
          {"key": "case", "label": "Case discussed", "type": "text", "required": True},
          {"key": "comment", "label": "Narrative feedback", "type": "textarea", "required": True}],
         "Structured discussion of a case the trainee managed, based on the written record."),
        ("MSF", "Multi-Source Feedback", AssessmentKind.MSF,
         [scale("clinical_care", "Clinical care"), scale("teamwork", "Working with colleagues"),
          scale("communication", "Communication with patients"),
          scale("reliability", "Reliability and punctuality"),
          scale("respect", "Respect for others"), scale("overall", "Overall professionalism", 2.0),
          {"key": "strength", "label": "Particular strength", "type": "textarea"},
          {"key": "development", "label": "Area for development", "type": "textarea"}],
         "Anonymous feedback from at least eight colleagues across disciplines."),
        ("ROT-END", "End of Rotation Assessment", AssessmentKind.ROTATION_END,
         [scale("knowledge", "Clinical knowledge"), scale("skills", "Practical skills", 1.5),
          scale("judgement", "Clinical judgement", 1.5),
          scale("professionalism", "Professionalism"), scale("teaching", "Teaching contribution"),
          scale("research", "Academic contribution"),
          scale("overall", "Overall performance this rotation", 2.5),
          {"key": "progress", "label": "Progress against rotation objectives",
           "type": "textarea", "required": True}],
         "Summative supervisor judgement at the close of a posting."),
        ("HO-END", "House Officer End-of-Posting Assessment", AssessmentKind.ROTATION_END,
         [scale("clinical", "Clinical competence", 2.0), scale("procedures", "Practical procedures"),
          scale("records", "Record keeping"), scale("attendance", "Attendance and punctuality"),
          scale("attitude", "Attitude to patients and staff", 1.5),
          scale("overall", "Overall fitness to progress", 2.5),
          {"key": "recommendation", "label": "Recommendation", "type": "select",
           "options": ["Satisfactory", "Satisfactory with reservations", "Unsatisfactory"],
           "required": True}],
         "MDCN end-of-posting sign-off for house officers."),
    ]

    for code, name, kind, schema, instructions in specs:
        template = AssessmentTemplate(
            tenant_id=tenant.id,
            code=code,
            name=name,
            kind=kind,
            instructions=instructions,
            form_schema=schema,
            scoring_config={
                "method": "weighted_mean", "scale_max": 9, "pass_mark": 55,
                "verdict_bands": {"below_expectation": 0, "borderline": 45,
                                  "meets_expectation": 60, "above_expectation": 78,
                                  "outstanding": 90},
            },
            min_assessors=8 if kind == AssessmentKind.MSF else 1,
            is_anonymous=kind == AssessmentKind.MSF,
        )
        db.add(template)
        templates[code] = template
    db.flush()
    log(f"assessment instruments: {len(templates)}")
    return templates


def build_procedure_catalogue(db: Session, tenant: Tenant,
                              specialties: dict[str, Specialty]) -> dict[str, ProcedureCatalogueItem]:
    existing = {
        p.code: p
        for p in db.execute(
            select(ProcedureCatalogueItem).where(ProcedureCatalogueItem.tenant_id == tenant.id)
        ).scalars()
    }
    if existing:
        return existing

    group_to_specialty = {
        "Surgery": "SURG", "Medicine": "MED", "Obstetrics & Gynaecology": "OBGYN",
        "Paediatrics": "PAED", "Anaesthesia": "ANAES", "Dentistry": "DENT",
    }
    for group, items in PROCEDURES.items():
        specialty = specialties.get(group_to_specialty.get(group, ""))
        for code, name, category, grade in items:
            item = ProcedureCatalogueItem(
                tenant_id=tenant.id,
                specialty_id=specialty.id if specialty else None,
                code=code, name=name, category=category, grade=grade,
            )
            db.add(item)
            existing[code] = item
    db.flush()
    log(f"procedure catalogue: {len(existing)} procedures")
    return existing


# ==========================================================================
# Trainees, activity and evidence
# ==========================================================================
def enrol_cohort(db: Session, tenant: Tenant, units: dict[str, OrgUnit], people: dict[str, User],
                 programme: Programme, version: CurriculumVersion,
                 cohort: list[tuple], unit_code: str,
                 supervisor_locals: list[str]) -> list[Enrolment]:
    enrolments: list[Enrolment] = []
    today = date.today()

    for index, (local, _title, _first, _last, level, year) in enumerate(cohort):
        trainee = people[local]
        existing = db.execute(
            select(Enrolment).where(
                Enrolment.trainee_id == trainee.id, Enrolment.programme_id == programme.id
            )
        ).scalar_one_or_none()
        if existing is not None:
            enrolments.append(existing)
            continue

        start = today - relativedelta(months=12 * (year - 1) + rng.randint(1, 6))
        supervisor = people[supervisor_locals[index % len(supervisor_locals)]]
        enrolment = Enrolment(
            tenant_id=tenant.id,
            trainee_id=trainee.id,
            programme_id=programme.id,
            curriculum_version_id=version.id,
            org_unit_id=units[unit_code].id,
            primary_supervisor_id=supervisor.id,
            registration_number=f"{programme.code}/{start.year}/{index + 1:03d}",
            college_number=f"{(programme.awarding_body or 'col').upper()}/{rng.randint(10000, 99999)}",
            cohort_year=start.year,
            current_level=level,
            current_year=year,
            start_date=start,
            expected_end_date=start + relativedelta(months=programme.duration_months),
        )
        db.add(enrolment)
        db.flush()

        try:
            planned = rotation_engine.plan_schedule(db, enrolment, to_year=min(year + 1, 4))
            rotation_engine.materialise(db, enrolment, planned)
            db.flush()
            # Close everything already in the past so promotion gates behave realistically.
            for rotation in enrolment.rotations:
                if rotation.end_date < today:
                    rotation.status = "completed"
                    rotation.completion_percent = float(rng.randint(82, 100))
                    rotation.closed_at = utcnow()
                    rotation.supervisor_comment = "Objectives met; progressing well."
                    db.add(rotation)
        except rotation_engine.RotationPlanningError as exc:
            log(f"    ! could not plan rotations for {trainee.display_name}: {exc}")

        enrolments.append(enrolment)

    db.flush()
    return enrolments


def build_academic_calendar(db: Session, tenant: Tenant, units: dict[str, OrgUnit],
                            people: dict[str, User], enrolments: list[Enrolment]) -> int:
    """A year of weekly departmental academic activity, with realistic attendance."""
    if db.execute(
        select(AcademicActivity).where(AcademicActivity.tenant_id == tenant.id).limit(1)
    ).scalar_one_or_none() is not None:
        return 0

    today = date.today()
    start = today - timedelta(days=364)
    schedule = [
        (AcademicActivityKind.GRAND_ROUND, "Grand Round", 0, 2.0),        # Monday
        (AcademicActivityKind.JOURNAL_CLUB, "Journal Club", 2, 1.5),      # Wednesday
        (AcademicActivityKind.MORTALITY_MEETING, "Mortality Review", 4, 1.5),
        (AcademicActivityKind.SEMINAR, "Departmental Seminar", 3, 1.0),
    ]
    departments = ["DEPT-SURG", "DEPT-MED"]
    presenters = {
        "DEPT-SURG": ["consultant1", "consultant2", "consultant3", "hod.surgery"],
        "DEPT-MED": ["consultant4", "consultant5", "hod.medicine"],
    }
    topics = [
        "Damage control in abdominal trauma", "Antimicrobial stewardship on the wards",
        "Enhanced recovery after surgery", "Managing diabetic ketoacidosis",
        "Surgical site infection prevention", "Acute kidney injury: recognition and response",
        "Blood transfusion practice and safety", "Sepsis: the first hour",
        "Perioperative anticoagulation", "Nutrition in the surgical patient",
        "Delirium in the hospitalised patient", "Interpreting the acute abdomen on CT",
    ]

    created = 0
    activities: list[AcademicActivity] = []
    cursor = start
    while cursor < today:
        for kind, label, weekday, credits in schedule:
            occurrence = cursor + timedelta(days=(weekday - cursor.weekday()) % 7)
            if occurrence >= today:
                continue
            if kind == AcademicActivityKind.MORTALITY_MEETING and occurrence.day > 7:
                continue  # monthly
            for dept_code in departments:
                presenter_local = rng.choice(presenters[dept_code])
                activity = AcademicActivity(
                    tenant_id=tenant.id,
                    org_unit_id=units[dept_code].id,
                    kind=kind,
                    title=f"{label}: {rng.choice(topics)}",
                    scheduled_at=datetime.combine(occurrence, time(8, 0)),
                    scheduled_on=occurrence,
                    duration_minutes=60 if kind != AcademicActivityKind.GRAND_ROUND else 90,
                    venue=f"{units[dept_code].name} Seminar Room",
                    presenter_id=people[presenter_local].id,
                    expected_levels=["registrar", "senior_registrar", "house_officer"],
                    is_mandatory=True,
                    cme_credits=credits,
                    series_code=f"{dept_code}-{kind}",
                    status="completed",
                )
                db.add(activity)
                activities.append(activity)
                created += 1
        cursor += timedelta(days=7)
    db.flush()

    # Attendance: each trainee attends their own department's sessions at a
    # trainee-specific rate, so the analytics show genuine variation.
    for enrolment in enrolments:
        rate = rng.uniform(0.55, 0.96)
        for activity in activities:
            if activity.org_unit_id != enrolment.org_unit_id:
                # Trainees are also expected at the sessions of the department they are
                # rotating through; the demo keeps it to the parent department.
                continue
            if activity.scheduled_on < enrolment.start_date:
                continue
            attended = rng.random() < rate
            role = ParticipantRole.ATTENDEE
            if attended and rng.random() < 0.06:
                role = ParticipantRole.PRESENTER

            db.add(
                ActivityParticipant(
                    tenant_id=tenant.id, activity_id=activity.id, user_id=enrolment.trainee_id,
                    role=role, attended=attended,
                    checked_in_at=datetime.combine(activity.scheduled_on, time(8, 2))
                    if attended else None,
                    credits_awarded=activity.cme_credits * (2.0 if role == ParticipantRole.PRESENTER else 1.0)
                    if attended else 0.0,
                )
            )
            db.add(
                AttendanceRecord(
                    tenant_id=tenant.id, user_id=enrolment.trainee_id, activity_id=activity.id,
                    recorded_for=activity.scheduled_on,
                    status=AttendanceStatus.PRESENT if attended else AttendanceStatus.ABSENT,
                    capture_method="qr",
                )
            )
            if attended:
                db.add(
                    CmeCreditLedger(
                        tenant_id=tenant.id, user_id=enrolment.trainee_id,
                        period_year=activity.scheduled_on.year,
                        source_kind="academic_activity", source_id=activity.id,
                        description=activity.title,
                        credits=activity.cme_credits * (2.0 if role == ParticipantRole.PRESENTER else 1.0),
                        awarded_on=activity.scheduled_on,
                    )
                )
    db.flush()
    log(f"academic calendar: {created} sessions with attendance for {len(enrolments)} trainees")
    return created


def build_duty_rosters(db: Session, tenant: Tenant, units: dict[str, OrgUnit],
                       people: dict[str, User], enrolments: list[Enrolment]) -> int:
    """Twelve months of call rosters with attendance, so the duty metrics are real."""
    from app.models.duty import DutyRoster, DutyShift
    from app.models.enums import DutyKind, ShiftStatus

    if db.execute(
        select(DutyRoster).where(DutyRoster.tenant_id == tenant.id).limit(1)
    ).scalar_one_or_none() is not None:
        return 0

    today = date.today()
    shifts_created = 0
    by_unit: dict[str, list[Enrolment]] = {}
    for enrolment in enrolments:
        by_unit.setdefault(enrolment.org_unit_id, []).append(enrolment)

    for org_unit_id, cohort in by_unit.items():
        if not cohort:
            continue
        # One monthly roster per department for the last twelve months.
        for months_back in range(12, 0, -1):
            period_start = (today.replace(day=1) - relativedelta(months=months_back))
            period_end = period_start + relativedelta(months=1) - timedelta(days=1)
            roster = DutyRoster(
                tenant_id=tenant.id,
                org_unit_id=org_unit_id,
                name=f"Call roster — {period_start:%B %Y}",
                period_start=period_start,
                period_end=period_end,
                status="approved",
                published_at=utcnow(),
                generation_config={"min_rest_hours": 11, "max_consecutive_nights": 3,
                                   "calls_per_person_per_month": 6},
            )
            db.add(roster)
            db.flush()

            cursor = period_start
            index = 0
            while cursor <= period_end:
                # Two people on each night: a first-on and a second-on call.
                for slot in range(2):
                    enrolment = cohort[(index + slot) % len(cohort)]
                    if cursor < enrolment.start_date:
                        continue
                    is_weekend = cursor.weekday() >= 5
                    starts = datetime.combine(cursor, time(16, 0))
                    shift = DutyShift(
                        tenant_id=tenant.id,
                        roster_id=roster.id,
                        org_unit_id=org_unit_id,
                        user_id=enrolment.trainee_id,
                        duty_kind=DutyKind.WEEKEND if is_weekend else DutyKind.NIGHT_CALL,
                        starts_at=starts,
                        ends_at=starts + timedelta(hours=16),
                        location="Accident & Emergency" if slot == 0 else "Ward cover",
                        supervising_user_id=enrolment.primary_supervisor_id,
                        status=ShiftStatus.COMPLETED,
                        weight=1.4 if is_weekend else 1.0,
                    )
                    db.add(shift)
                    db.flush()
                    shifts_created += 1

                    # Attendance is high but imperfect, which is what makes the
                    # attendance analytics worth looking at.
                    roll = rng.random()
                    if roll < 0.90:
                        attendance_status = AttendanceStatus.PRESENT
                        late = 0
                    elif roll < 0.96:
                        attendance_status = AttendanceStatus.LATE
                        late = rng.randint(10, 55)
                    elif roll < 0.985:
                        attendance_status = AttendanceStatus.EXCUSED
                        late = 0
                    else:
                        attendance_status = AttendanceStatus.ABSENT
                        late = 0

                    db.add(
                        AttendanceRecord(
                            tenant_id=tenant.id,
                            user_id=enrolment.trainee_id,
                            shift_id=shift.id,
                            recorded_for=cursor,
                            status=attendance_status,
                            check_in_at=starts + timedelta(minutes=late)
                            if attendance_status != AttendanceStatus.ABSENT else None,
                            check_out_at=starts + timedelta(hours=16)
                            if attendance_status not in {AttendanceStatus.ABSENT} else None,
                            minutes_late=late,
                            capture_method="geo",
                            excuse_reason="Approved study leave"
                            if attendance_status == AttendanceStatus.EXCUSED else None,
                            excuse_approved=attendance_status == AttendanceStatus.EXCUSED,
                        )
                    )
                cursor += timedelta(days=1)
                index += 1

    db.flush()
    log(f"duty: {shifts_created} call shifts rostered with attendance")
    return shifts_created


SURGICAL_DIAGNOSES = [
    ("Acute appendicitis", "APPEND"), ("Inguinal hernia", "HERN-ING"),
    ("Cholelithiasis", "CHOLE-LAP"), ("Intestinal obstruction", "LAPAROT"),
    ("Multinodular goitre", "THYROID"), ("Breast carcinoma", "MASTECT"),
    ("Haemorrhoids", "HAEMORR"), ("Perforated peptic ulcer", "LAPAROT"),
    ("Soft tissue abscess", "ID-ABSCESS"), ("Burn wound", "DEBRIDE"),
    ("Urinary retention", "URETH-CATH"), ("Pneumothorax", "CHEST-DRAIN"),
]

MEDICAL_DIAGNOSES = [
    ("Community-acquired pneumonia", None), ("Diabetic ketoacidosis", None),
    ("Acute kidney injury", "HAEMODIAL"), ("Stroke", "LUMBAR-P"),
    ("Heart failure", "ECHO"), ("Pleural effusion", "PLEURAL-T"),
    ("Decompensated liver disease", "ASCITIC-T"), ("Severe malaria", None),
    ("Hypertensive emergency", None), ("Upper GI bleeding", "ENDOSCOPY"),
]


def build_logbooks(db: Session, tenant: Tenant, people: dict[str, User],
                   enrolments: list[Enrolment], procedures: dict[str, ProcedureCatalogueItem],
                   surgical: bool = True) -> int:
    today = date.today()
    total = 0
    diagnoses = SURGICAL_DIAGNOSES if surgical else MEDICAL_DIAGNOSES

    for enrolment in enrolments:
        # Idempotent per enrolment, so a partially-seeded database can be topped up.
        if db.execute(
            select(LogEntry).where(LogEntry.enrolment_id == enrolment.id).limit(1)
        ).scalar_one_or_none() is not None:
            continue

        # Productivity varies by trainee so the dashboards are not uniform.
        weekly_rate = rng.uniform(3.5, 9.0)
        days = max(1, (today - enrolment.start_date).days)
        count = int(days / 7 * weekly_rate)
        supervisor_id = enrolment.primary_supervisor_id

        for _ in range(count):
            offset = rng.randint(0, max(1, days - 1))
            occurred = enrolment.start_date + timedelta(days=offset)
            if occurred > today:
                continue
            rotation = enrolment.current_rotation(occurred)

            entry_type = rng.choices(
                [LogEntryType.ADMISSION, LogEntryType.MAJOR_PROCEDURE,
                 LogEntryType.MINOR_PROCEDURE, LogEntryType.CLINIC,
                 LogEntryType.WARD_ROUND, LogEntryType.EMERGENCY_CALL,
                 LogEntryType.CONSULTATION],
                weights=[28, 18, 12, 15, 10, 12, 5],
            )[0]

            diagnosis, procedure_code = rng.choice(diagnoses)
            procedure = procedures.get(procedure_code) if procedure_code else None
            is_procedure = entry_type in {LogEntryType.MAJOR_PROCEDURE, LogEntryType.MINOR_PROCEDURE}

            if is_procedure and procedure is None:
                procedure = rng.choice(list(procedures.values()))

            # Participation shifts toward independence with seniority.
            if enrolment.current_year <= 1:
                role_weights = [30, 45, 20, 5, 0]
            elif enrolment.current_year == 2:
                role_weights = [10, 35, 40, 14, 1]
            elif enrolment.current_year == 3:
                role_weights = [4, 20, 40, 32, 4]
            else:
                role_weights = [2, 10, 28, 48, 12]

            participation = rng.choices(
                [ParticipationRole.OBSERVED, ParticipationRole.ASSISTED,
                 ParticipationRole.PERFORMED_SUPERVISED, ParticipationRole.PERFORMED_INDEPENDENT,
                 ParticipationRole.SUPERVISED_OTHER],
                weights=role_weights,
            )[0] if is_procedure else None

            # Most entries are validated; a realistic minority sit pending or queried.
            roll = rng.random()
            if roll < 0.86:
                validation = ValidationStatus.VALIDATED
            elif roll < 0.96:
                validation = ValidationStatus.PENDING
            elif roll < 0.99:
                validation = ValidationStatus.QUERIED
            else:
                validation = ValidationStatus.REJECTED

            entry = LogEntry(
                tenant_id=tenant.id,
                enrolment_id=enrolment.id,
                rotation_assignment_id=rotation.id if rotation else None,
                org_unit_id=rotation.org_unit_id if rotation else enrolment.org_unit_id,
                entry_type=entry_type,
                occurred_at=datetime.combine(occurred, time(rng.randint(7, 20), rng.choice([0, 15, 30, 45]))),
                occurred_on=occurred,
                title=(procedure.name if is_procedure and procedure else diagnosis),
                summary=f"{diagnosis} — managed on the {['ward', 'emergency unit', 'theatre list', 'clinic'][rng.randint(0, 3)]}.",
                patient_reference=f"PSN-{rng.randint(100000, 999999)}",
                patient_age_years=rng.randint(1, 88),
                patient_sex=rng.choice(["male", "female"]),
                setting=rng.choice(["ward", "theatre", "clinic", "emergency"]),
                diagnosis=diagnosis,
                procedure_id=procedure.id if is_procedure and procedure else None,
                procedure_name=procedure.name if is_procedure and procedure else None,
                procedure_grade=procedure.grade if is_procedure and procedure else None,
                participation_role=participation,
                complexity=rng.choices(
                    [CaseComplexity.ROUTINE, CaseComplexity.INTERMEDIATE,
                     CaseComplexity.COMPLEX, CaseComplexity.HIGHLY_COMPLEX],
                    weights=[50, 32, 15, 3],
                )[0],
                outcome=rng.choices(
                    [CaseOutcome.UNEVENTFUL, CaseOutcome.MINOR_COMPLICATION,
                     CaseOutcome.MAJOR_COMPLICATION, CaseOutcome.MORTALITY],
                    weights=[86, 9, 4, 1],
                )[0],
                duration_minutes=rng.randint(20, 240) if is_procedure else None,
                supervisor_id=(rotation.supervisor_id if rotation else supervisor_id),
                validation_status=validation,
                validated_at=utcnow() if validation == ValidationStatus.VALIDATED else None,
                validated_by_id=(rotation.supervisor_id if rotation else supervisor_id)
                if validation == ValidationStatus.VALIDATED else None,
                query_count=1 if validation == ValidationStatus.QUERIED else 0,
                reflection=("Reviewed the anatomy beforehand; the retrograde approach made "
                            "the dissection safer." if rng.random() < 0.25 else None),
            )
            db.add(entry)
            total += 1

        # Teaching records feed the Teaching Score.
        for _ in range(rng.randint(4, 18)):
            occurred = enrolment.start_date + timedelta(days=rng.randint(0, max(1, days - 1)))
            if occurred > today:
                continue
            db.add(
                TeachingRecord(
                    tenant_id=tenant.id,
                    enrolment_id=enrolment.id,
                    title=rng.choice([
                        "Bedside teaching: abdominal examination",
                        "Tutorial: fluid and electrolyte balance",
                        "Skills session: suturing",
                        "Seminar: interpreting arterial blood gases",
                    ]),
                    audience="medical students",
                    audience_size=rng.randint(4, 25),
                    occurred_on=occurred,
                    duration_minutes=rng.choice([45, 60, 90]),
                    feedback_score=round(rng.uniform(3.4, 5.0), 1),
                    validation_status=ValidationStatus.VALIDATED,
                    verified_by_id=supervisor_id,
                )
            )

    db.flush()
    log(f"logbook: {total} entries across {len(enrolments)} trainees")
    return total


def build_assessments(db: Session, tenant: Tenant, people: dict[str, User],
                      enrolments: list[Enrolment], templates: dict[str, AssessmentTemplate],
                      version: CurriculumVersion) -> int:
    from app.api.v1.endpoints.assessments import score_responses

    today = date.today()
    competencies = list(
        db.execute(
            select(Competency).where(Competency.curriculum_version_id == version.id)
        ).scalars()
    )
    epas = [c for c in competencies if c.is_epa]
    total = 0

    for enrolment in enrolments:
        if db.execute(
            select(Assessment).where(Assessment.enrolment_id == enrolment.id).limit(1)
        ).scalar_one_or_none() is not None:
            continue

        # Ability is a per-trainee latent value; assessments cluster around it.
        ability = rng.uniform(4.6, 8.2)
        days = max(1, (today - enrolment.start_date).days)
        count = max(6, int(days / 30) * rng.randint(1, 2))

        for _ in range(count):
            code = rng.choice(["MINI-CEX", "DOPS", "CBD"])
            template = templates[code]
            occurred = enrolment.start_date + timedelta(days=rng.randint(0, max(1, days - 1)))
            if occurred > today:
                continue
            rotation = enrolment.current_rotation(occurred)
            assessor_id = (rotation.supervisor_id if rotation else enrolment.primary_supervisor_id)

            responses: dict = {}
            for field in template.form_schema:
                if field["type"] == "scale":
                    value = round(min(9, max(1, rng.gauss(ability, 1.0))))
                    responses[field["key"]] = value
                elif field["type"] in {"text", "textarea"}:
                    responses[field["key"]] = rng.choice([
                        "Structured, safe and appropriate for stage.",
                        "Sound decision-making; continue building operative confidence.",
                        "Good rapport with the patient; tighten the differential.",
                    ])
                elif field["type"] == "select":
                    responses[field["key"]] = field["options"][0]

            raw, maximum, percent, verdict, is_pass = score_responses(template, responses)
            db.add(
                Assessment(
                    tenant_id=tenant.id,
                    template_id=template.id,
                    enrolment_id=enrolment.id,
                    rotation_assignment_id=rotation.id if rotation else None,
                    assessor_id=assessor_id,
                    occurred_on=occurred,
                    setting=rng.choice(["ward", "theatre", "clinic", "emergency unit"]),
                    responses=responses,
                    raw_score=raw, max_score=maximum, percent_score=percent,
                    verdict=verdict, is_pass=is_pass,
                    strengths="Thorough and safe; good communication with the team.",
                    development_needs="Continue building independent operative decision-making.",
                    agreed_actions="Book two further DOPS this rotation.",
                    status="approved",
                    submitted_at=utcnow(),
                )
            )
            total += 1

        # Entrustment ratings, scaled to seniority and latent ability.
        for competency in competencies:
            base = {1: 2, 2: 2, 3: 3, 4: 4}.get(enrolment.current_year, 2)
            adjustment = 1 if ability > 7.0 else (-1 if ability < 5.4 else 0)
            level_value = max(1, min(5, base + adjustment + rng.choice([0, 0, 0, 1, -1])))
            level = next(
                name for name, value in ENTRUSTMENT_ORDER.items() if value == level_value
            )
            db.add(
                CompetencyRating(
                    tenant_id=tenant.id,
                    enrolment_id=enrolment.id,
                    competency_id=competency.id,
                    assessor_id=enrolment.primary_supervisor_id,
                    level=level,
                    level_value=level_value,
                    rated_on=today - timedelta(days=rng.randint(5, 90)),
                    evidence="Judged across multiple observed encounters this rotation.",
                )
            )

    db.flush()
    log(f"assessments: {total} completed, with entrustment ratings on "
        f"{len(competencies)} competencies ({len(epas)} EPAs)")
    return total


RESEARCH_TITLES = [
    ("Outcomes of laparoscopic versus open appendicectomy at a Nigerian teaching hospital",
     ["laparoscopy", "general surgery", "outcomes"]),
    ("Surgical site infection rates following emergency laparotomy: a prospective cohort",
     ["infection", "trauma", "cohort study"]),
    ("Pattern and outcome of paediatric burn injuries over five years",
     ["burns", "paediatric surgery", "epidemiology"]),
    ("Prevalence of chronic kidney disease among hypertensive outpatients",
     ["nephrology", "hypertension", "epidemiology"]),
    ("Antimicrobial resistance patterns in surgical wound isolates",
     ["infectious diseases", "microbiology", "antimicrobial stewardship"]),
    ("Delay to presentation in breast cancer: a mixed-methods study",
     ["oncology", "breast surgery", "qualitative research"]),
    ("Quality of life after thyroidectomy for multinodular goitre",
     ["endocrine", "quality of life", "cohort study"]),
    ("Predictors of mortality in diabetic ketoacidosis admissions",
     ["endocrinology", "critical care", "outcomes"]),
]


def build_research(db: Session, tenant: Tenant, people: dict[str, User],
                   enrolments: list[Enrolment]) -> int:
    from app.services.allocation import assign_research_supervisor

    today = date.today()
    stages = list(DissertationStage)
    created = 0

    for index, enrolment in enumerate(enrolments):
        if enrolment.current_year < 2:
            continue
        if db.execute(
            select(ResearchProject).where(ResearchProject.enrolment_id == enrolment.id).limit(1)
        ).scalar_one_or_none() is not None:
            continue

        title, keywords = RESEARCH_TITLES[index % len(RESEARCH_TITLES)]
        # Progress broadly tracks seniority.
        target_stage_index = {2: 4, 3: 6, 4: 11}.get(enrolment.current_year, 3)
        target_stage_index = max(1, min(len(stages) - 2,
                                        target_stage_index + rng.choice([-1, 0, 0, 1])))
        current_stage = stages[target_stage_index]

        project = ResearchProject(
            tenant_id=tenant.id,
            org_unit_id=enrolment.org_unit_id,
            enrolment_id=enrolment.id,
            principal_investigator_id=enrolment.trainee_id,
            title=title,
            research_type="dissertation",
            submitting_body="npmcn",
            aim="To determine the outcomes and identify modifiable predictors of poor result.",
            objectives=["Describe the study population",
                        "Determine the primary outcome rate",
                        "Identify independent predictors of the outcome"],
            study_design=rng.choice(["prospective cohort", "retrospective review",
                                     "cross-sectional study"]),
            setting="University Teaching Hospital (Demo)",
            sample_size=rng.randint(80, 420),
            keywords=keywords,
            current_stage=current_stage,
            status="approved",
            started_on=enrolment.start_date + timedelta(days=rng.randint(120, 300)),
            target_completion_on=enrolment.expected_end_date - timedelta(days=90),
            ethics_status="approved" if target_stage_index >= 5 else "submitted",
            ethics_reference=f"UTH/HREC/{rng.randint(100, 999)}/2026" if target_stage_index >= 5 else None,
            ethics_approved_on=today - timedelta(days=rng.randint(60, 400)) if target_stage_index >= 5 else None,
        )
        db.add(project)
        db.flush()

        try:
            assign_research_supervisor(db, project)
        except ValueError:
            db.add(
                ProjectSupervision(
                    tenant_id=tenant.id, project_id=project.id,
                    supervisor_id=enrolment.primary_supervisor_id, is_primary=True,
                    assigned_on=project.started_on or today, allocation_method="manual",
                )
            )

        milestone_specs = [
            (DissertationStage.SUPERVISOR_ASSIGNMENT, "Supervisor assignment"),
            (DissertationStage.TOPIC_APPROVAL, "Topic approval"),
            (DissertationStage.PROPOSAL_WRITING, "Proposal writing"),
            (DissertationStage.PROPOSAL_DEFENCE, "Proposal defence"),
            (DissertationStage.ETHICS_APPROVAL, "Ethics approval"),
            (DissertationStage.DATA_COLLECTION, "Data collection"),
            (DissertationStage.ANALYSIS, "Data analysis"),
            (DissertationStage.DRAFT_SUBMISSION, "Draft submission"),
            (DissertationStage.CORRECTIONS, "Corrections"),
            (DissertationStage.FINAL_DEFENCE, "Final defence"),
            (DissertationStage.COLLEGE_SUBMISSION, "College submission"),
        ]
        base = project.started_on or today
        approved = 0
        for seq, (stage, label) in enumerate(milestone_specs):
            reached = stages.index(stage) <= target_stage_index
            if reached:
                approved += 1
            db.add(
                DissertationMilestone(
                    tenant_id=tenant.id, project_id=project.id, stage=stage, sequence=seq,
                    title=label, due_on=base + timedelta(days=60 * seq),
                    completed_on=base + timedelta(days=60 * seq) if reached else None,
                    status="approved" if reached else "draft",
                    approver_id=enrolment.primary_supervisor_id if reached else None,
                )
            )
        project.progress_percent = round(approved / len(milestone_specs) * 100, 1)

        if enrolment.current_year >= 3 and rng.random() < 0.6:
            db.add(
                Publication(
                    tenant_id=tenant.id,
                    user_id=enrolment.trainee_id,
                    enrolment_id=enrolment.id,
                    project_id=project.id,
                    publication_type=rng.choice(["journal_article", "conference_abstract"]),
                    title=title,
                    authors=f"{people[next(k for k, v in people.items() if v.id == enrolment.trainee_id)].display_name} et al.",
                    author_position=1,
                    is_corresponding=True,
                    venue=rng.choice(["Nigerian Journal of Surgery",
                                      "West African Journal of Medicine",
                                      "Annals of African Medicine"]),
                    year=today.year - rng.randint(0, 1),
                    doi=f"10.4314/njs.v{rng.randint(20, 32)}i{rng.randint(1, 4)}.{rng.randint(1, 20)}",
                    indexed_in=["ajol", "google_scholar"],
                    is_peer_reviewed=True,
                    verification_status="approved",
                )
            )
        created += 1

    db.flush()
    log(f"research: {created} dissertations with supervisors, milestones and publications")
    return created


def compute_scorecards(db: Session, enrolments: list[Enrolment]) -> None:
    for enrolment in enrolments:
        scoring.score_and_persist(db, enrolment, trigger="scheduled")
    db.flush()
    log(f"scorecards: {len(enrolments)} computed "
        f"({sum(1 for e in enrolments if e.latest_rag == 'green')} green, "
        f"{sum(1 for e in enrolments if e.latest_rag == 'amber')} amber, "
        f"{sum(1 for e in enrolments if e.latest_rag == 'red')} red)")


# ==========================================================================
# Orchestration
# ==========================================================================
def seed(*, demo: bool = True, reset: bool = False) -> None:
    if settings.is_production:
        raise SystemExit(
            "Refusing to seed a production environment. Unset RTC_ENV=production to proceed."
        )
    if demo and not settings.allow_demo_seed:
        raise SystemExit("Demo seeding is disabled (RTC_ALLOW_DEMO_SEED=false).")

    if reset:
        print("Dropping all tables …")
        Base.metadata.drop_all(bind=engine)

    print("Ensuring schema …")
    Base.metadata.create_all(bind=engine)

    db: Session = SessionLocal()
    try:
        print("\nPlatform reference data")
        seed_permissions(db)
        roles = seed_roles(db)
        specialties = seed_specialties(db)
        seed_accreditation_profiles(db)
        db.commit()

        if not demo:
            print("\nReference data seeded. Skipping demo institution.")
            return

        print("\nDemo institution")
        tenant = build_tenant(db)
        units = build_org_tree(db, tenant, specialties)
        people = build_people(db, tenant, units, roles)
        templates = build_assessment_templates(db, tenant)
        procedures = build_procedure_catalogue(db, tenant, specialties)
        db.commit()

        print("\nCurricula")
        surgery_programme, surgery_version = build_surgery_curriculum(db, tenant, units, specialties)
        medicine_programme, medicine_version = build_medicine_curriculum(db, tenant, units, specialties)
        house_programme, house_version = build_housemanship(db, tenant, units)
        db.commit()

        print("\nCohorts")
        surgery_enrolments = enrol_cohort(
            db, tenant, units, people, surgery_programme, surgery_version,
            SURGERY_TRAINEES, "DEPT-SURG",
            ["consultant1", "consultant2", "consultant3", "hod.surgery"],
        )
        medicine_enrolments = enrol_cohort(
            db, tenant, units, people, medicine_programme, medicine_version,
            MEDICINE_TRAINEES, "DEPT-MED", ["consultant4", "consultant5", "hod.medicine"],
        )
        house_enrolments = enrol_cohort(
            db, tenant, units, people, house_programme, house_version,
            HOUSE_OFFICERS, "DEPT-SURG", ["consultant1", "consultant2"],
        )
        all_enrolments = surgery_enrolments + medicine_enrolments + house_enrolments
        db.commit()
        log(f"enrolments: {len(all_enrolments)} trainees with generated rotation schedules")

        print("\nActivity and evidence")
        build_academic_calendar(db, tenant, units, people, all_enrolments)
        db.commit()

        build_duty_rosters(db, tenant, units, people, all_enrolments)
        db.commit()

        build_logbooks(db, tenant, people, surgery_enrolments + house_enrolments, procedures,
                       surgical=True)
        build_logbooks(db, tenant, people, medicine_enrolments, procedures, surgical=False)
        db.commit()

        build_assessments(db, tenant, people, surgery_enrolments, templates, surgery_version)
        build_assessments(db, tenant, people, medicine_enrolments, templates, medicine_version)
        build_assessments(db, tenant, people, house_enrolments, templates, house_version)
        db.commit()

        build_research(db, tenant, people, surgery_enrolments + medicine_enrolments)
        db.commit()

        print("\nAnalytics")
        compute_scorecards(db, all_enrolments)
        db.commit()

        print("\n" + "=" * 74)
        print("  Seed complete.")
        print("=" * 74)
        print(f"  Institution : {tenant.name} [{tenant.code}]")
        print(f"  Database    : {settings.database_url}")
        print(f"  Password    : {DEMO_PASSWORD} (all demo accounts)")
        print("\n  Sign in as:")
        for label, email in [
            ("National Super Administrator", "super@rtc.health"),
            ("Chief Medical Director", "cmd@uthdemo.health"),
            ("Director of Residency Training", "drt@uthdemo.health"),
            ("Head of Department, Surgery", "hod.surgery@uthdemo.health"),
            ("Consultant Surgeon", "consultant1@uthdemo.health"),
            ("Senior Registrar", "snr.registrar1@uthdemo.health"),
            ("Registrar", "registrar1@uthdemo.health"),
            ("House Officer", "houseofficer1@uthdemo.health"),
        ]:
            print(f"    {label:<32} {email}")
        print("=" * 74 + "\n")

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the RTC database.")
    parser.add_argument("--no-demo", action="store_true",
                        help="Load platform reference data only, without the demo institution.")
    parser.add_argument("--reset", action="store_true",
                        help="Drop every table first. Destroys all data.")
    args = parser.parse_args()

    if args.reset:
        confirm = input("This will DROP ALL TABLES. Type 'yes' to continue: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            sys.exit(1)

    seed(demo=not args.no_demo, reset=args.reset)


if __name__ == "__main__":
    main()
