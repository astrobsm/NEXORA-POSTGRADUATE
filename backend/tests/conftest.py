"""Shared pytest fixtures.

Tests run against a throwaway SQLite database seeded with reference data plus a
minimal institution â€” small enough to be fast, complete enough to exercise the
engines end to end.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

# Point the application at a temporary database *before* app modules are imported.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="rtc-tests-"))
os.environ["RTC_DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test.db').as_posix()}"
os.environ["RTC_ENV"] = "local"
os.environ["RTC_SECRET_KEY"] = "test-secret-key-not-for-production"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.rbac import DEFAULT_ROLES  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.academic import AcademicActivity, ActivityParticipant  # noqa: E402
from app.models.assessment import AssessmentTemplate  # noqa: E402
from app.models.curriculum import (  # noqa: E402
    Competency,
    CurriculumVersion,
    ProcedureCatalogueItem,
    Programme,
    RotationTemplate,
    TrainingYear,
)
from app.models.enums import (  # noqa: E402
    AcademicActivityKind,
    AssessmentKind,
    CompetencyDomain,
    CurriculumStatus,
    EntrustmentLevel,
    ProgrammeType,
    TrainingLevel,
    UserStatus,
)
from app.models.identity import Role, RoleAssignment, SupervisorProfile, User  # noqa: E402
from app.models.tenancy import OrgUnit, Tenant  # noqa: E402
from app.models.training import Enrolment  # noqa: E402

TEST_PASSWORD = "TestPassw0rd!2026"


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Generator[None, None, None]:
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _clean_tables() -> Generator[None, None, None]:
    """Truncate everything between tests.

    The ``institution`` fixture commits (the API client runs in its own session and must
    see the data), so isolation cannot come from a rolled-back transaction. Deleting in
    reverse dependency order keeps foreign keys satisfied.
    """
    yield
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())


@pytest.fixture
def db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------
# A minimal but complete institution
# --------------------------------------------------------------------------
@pytest.fixture
def institution(db: Session) -> dict:
    """Tenant, department, roles, a consultant, a registrar and an active curriculum."""
    tenant = Tenant(name="Test Teaching Hospital", code="TTH", slug="tth",
                    accrediting_bodies=["npmcn"])
    db.add(tenant)
    db.flush()

    hospital = OrgUnit(tenant_id=tenant.id, kind="hospital", name="Test Teaching Hospital",
                       code="TTH-ROOT", path="/TTH-ROOT", depth=0,
                       capacity={"operating_theatres": 3, "icu_beds": 5, "library_seats": 20})
    db.add(hospital)
    db.flush()
    department = OrgUnit(tenant_id=tenant.id, parent_id=hospital.id, kind="department",
                         name="Department of Surgery", code="TTH-SURG",
                         path="/TTH-ROOT/TTH-SURG", depth=1,
                         capacity={"operating_theatres": 2, "icu_beds": 4, "library_seats": 12})
    db.add(department)
    db.flush()

    roles: dict[str, Role] = {}
    for spec in DEFAULT_ROLES:
        role = Role(tenant_id=None, code=spec.code, name=spec.name, rank=spec.rank,
                    scope_kind=spec.scope_kind, is_system=True,
                    permission_codes=list(spec.permissions))
        db.add(role)
        roles[spec.code] = role
    db.flush()

    def make(local: str, first: str, last: str, role_code: str) -> User:
        user = User(tenant_id=tenant.id, email=f"{local}@tth.health", first_name=first,
                    last_name=last, title="Dr.", hashed_password=hash_password(TEST_PASSWORD),
                    status=UserStatus.ACTIVE)
        db.add(user)
        db.flush()
        db.add(RoleAssignment(user_id=user.id, role_id=roles[role_code].id,
                              org_unit_id=department.id, is_primary=True))
        return user

    hod = make("hod", "Ada", "Obi", "head_of_department")
    consultant = make("consultant", "Bola", "Ade", "consultant")
    registrar = make("registrar", "Chidi", "Eze", "registrar")
    junior = make("junior", "Dele", "Kola", "registrar")
    # Holds identity.role.manage / identity.assignment.manage, so the privilege-
    # escalation guards can be exercised rather than short-circuited by the
    # permission check that precedes them.
    director = make("director", "Ejiro", "Peters", "director_residency")
    db.add(SupervisorProfile(user_id=consultant.id, tenant_id=tenant.id,
                             expertise=["laparoscopy", "trauma"], max_supervisees=3,
                             max_clinical_trainees=4))
    db.add(SupervisorProfile(user_id=hod.id, tenant_id=tenant.id,
                             expertise=["oncology"], max_supervisees=2, max_clinical_trainees=4))
    db.flush()

    programme = Programme(tenant_id=tenant.id, org_unit_id=department.id, code="SURG",
                          name="Surgery Residency", programme_type=ProgrammeType.RESIDENCY_JUNIOR,
                          entry_level=TrainingLevel.REGISTRAR,
                          exit_level=TrainingLevel.SENIOR_REGISTRAR, duration_months=48,
                          awarding_body="wacs")
    db.add(programme)
    db.flush()

    version = CurriculumVersion(tenant_id=tenant.id, programme_id=programme.id, version="1.0",
                                title="Surgery Curriculum", status=CurriculumStatus.ACTIVE,
                                effective_from=date(2025, 1, 1))
    db.add(version)
    db.flush()

    years = {}
    for sequence in (1, 2):
        year = TrainingYear(tenant_id=tenant.id, curriculum_version_id=version.id,
                            sequence=sequence, name=f"Year {sequence}",
                            level=TrainingLevel.REGISTRAR, duration_months=12)
        db.add(year)
        db.flush()
        years[sequence] = year
        db.add(RotationTemplate(tenant_id=tenant.id, training_year_id=year.id,
                                org_unit_id=department.id, name="General Surgery",
                                code=f"GS-Y{sequence}", sequence=1, duration_weeks=26,
                                max_trainees=4))
        db.add(RotationTemplate(tenant_id=tenant.id, training_year_id=year.id,
                                org_unit_id=department.id, name="Trauma",
                                code=f"TR-Y{sequence}", sequence=2, duration_weeks=26,
                                max_trainees=4))
    db.flush()

    epa = Competency(tenant_id=tenant.id, curriculum_version_id=version.id, code="EPA-1",
                     title="Manage the acute abdomen", domain=CompetencyDomain.PATIENT_CARE,
                     is_epa=True,
                     target_by_year={"1": "2_direct_supervision", "2": "3_indirect_supervision"},
                     exit_target=EntrustmentLevel.INDEPENDENT)
    db.add(epa)
    db.flush()

    procedure = ProcedureCatalogueItem(tenant_id=tenant.id, code="APPEND",
                                       name="Appendicectomy", category="general", grade="major")
    db.add(procedure)

    template = AssessmentTemplate(
        tenant_id=tenant.id, code="MINI-CEX", name="Mini-CEX", kind=AssessmentKind.MINI_CEX,
        form_schema=[
            {"key": "history", "label": "History", "type": "scale", "min": 1, "max": 9,
             "weight": 1.0},
            {"key": "overall", "label": "Overall", "type": "scale", "min": 1, "max": 9,
             "weight": 2.0},
            {"key": "comment", "label": "Comment", "type": "textarea"},
        ],
        scoring_config={"scale_max": 9, "pass_mark": 55},
    )
    db.add(template)
    db.flush()

    start = date.today() - timedelta(days=400)
    enrolment = Enrolment(tenant_id=tenant.id, trainee_id=registrar.id,
                          programme_id=programme.id, curriculum_version_id=version.id,
                          org_unit_id=department.id, primary_supervisor_id=consultant.id,
                          cohort_year=start.year, current_level=TrainingLevel.REGISTRAR,
                          current_year=2, start_date=start,
                          expected_end_date=start + timedelta(days=365 * 4))
    db.add(enrolment)
    db.flush()

    activity = AcademicActivity(
        tenant_id=tenant.id, org_unit_id=department.id,
        kind=AcademicActivityKind.GRAND_ROUND, title="Grand Round: sepsis",
        scheduled_at=datetime.combine(date.today() - timedelta(days=7), time(8, 0)),
        scheduled_on=date.today() - timedelta(days=7), is_mandatory=True, cme_credits=2.0,
        checkin_code="ABC123",
    )
    db.add(activity)
    db.flush()
    db.add(ActivityParticipant(tenant_id=tenant.id, activity_id=activity.id,
                               user_id=registrar.id, role="attendee", attended=True,
                               credits_awarded=2.0))
    db.commit()

    return {
        "tenant": tenant, "hospital": hospital, "department": department, "roles": roles,
        "hod": hod, "consultant": consultant, "registrar": registrar, "junior": junior,
        "director": director,
        "programme": programme, "version": version, "years": years, "epa": epa,
        "procedure": procedure, "template": template, "enrolment": enrolment,
        "activity": activity,
    }


@pytest.fixture
def auth(client: TestClient):
    """Return a callable that signs a user in and yields Authorization headers."""

    def _login(email: str, password: str = TEST_PASSWORD) -> dict[str, str]:
        response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        token = response.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    return _login
