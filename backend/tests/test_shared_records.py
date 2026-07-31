"""Platform-level (shared) records must be visible to every institution.

Several tables use a NULL tenant to mean "shared with everyone": system roles, the
specialty catalogue, accreditation standards, default notification templates.

These tests exist because the natural way to write that filter —
``column.in_([tenant_id, None])`` — is silently wrong: SQL's ``IN`` never matches a
NULL, so every shared row vanishes and the failure looks like an empty list rather
than an error. The bug reached a running system once; it will not do so again.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import owned_or_shared
from app.models.analytics import AccreditationCriterion, AccreditationProfile
from app.models.curriculum import Specialty
from app.models.system import NotificationTemplate

API = "/api/v1"


class TestOwnedOrSharedHelper:
    def test_matches_both_owned_and_shared_rows(self, db: Session, institution: dict):
        from app.models.tenancy import Tenant

        tenant_id = institution["tenant"].id
        other = Tenant(name="Another Hospital", code="OTH", slug="oth")
        db.add(other)
        db.flush()

        db.add(Specialty(tenant_id=None, code="SHARED", name="Shared Specialty"))
        db.add(Specialty(tenant_id=tenant_id, code="LOCAL", name="Local Specialty"))
        db.add(Specialty(tenant_id=other.id, code="FOREIGN", name="Foreign"))
        db.flush()

        rows = db.execute(
            select(Specialty).where(owned_or_shared(Specialty.tenant_id, tenant_id))
        ).scalars().all()
        codes = {row.code for row in rows}

        assert "SHARED" in codes, "a NULL-tenant row must be visible to every institution"
        assert "LOCAL" in codes
        assert "FOREIGN" not in codes

    def test_the_naive_in_filter_would_have_dropped_shared_rows(
        self, db: Session, institution: dict
    ):
        """Pins the actual SQL semantics, so nobody 'simplifies' the helper away."""
        tenant_id = institution["tenant"].id
        db.add(Specialty(tenant_id=None, code="SHARED2", name="Shared"))
        db.flush()

        naive = db.execute(
            select(Specialty).where(Specialty.tenant_id.in_([tenant_id, None]))
        ).scalars().all()
        correct = db.execute(
            select(Specialty).where(owned_or_shared(Specialty.tenant_id, tenant_id))
        ).scalars().all()

        assert "SHARED2" not in {row.code for row in naive}
        assert "SHARED2" in {row.code for row in correct}


class TestSharedRecordsAreReachableThroughTheApi:
    def test_shared_specialties_are_listed(
        self, client: TestClient, institution: dict, db: Session, auth
    ):
        db.add(Specialty(tenant_id=None, code="PLATFORM-SURG", name="Platform Surgery"))
        db.commit()

        response = client.get(f"{API}/curriculum/specialties", headers=auth("hod@tth.health"))
        assert response.status_code == 200
        assert "PLATFORM-SURG" in {row["code"] for row in response.json()}

    def test_system_roles_are_listed(self, client: TestClient, institution: dict, auth):
        """Every role in the fixture is a NULL-tenant system role. An empty catalogue
        would make role assignment impossible for a new institution."""
        response = client.get(f"{API}/users/roles/catalogue", headers=auth("hod@tth.health"))
        assert response.status_code == 200
        codes = {row["code"] for row in response.json()}
        assert "consultant" in codes
        assert "head_of_department" in codes
        assert len(codes) > 20

    def test_shared_accreditation_profiles_are_listed(
        self, client: TestClient, institution: dict, db: Session, auth
    ):
        profile = AccreditationProfile(
            tenant_id=None, body="npmcn", body_name="NPMCN", code="SHARED-STD",
            name="Shared Standard", version="1.0",
        )
        db.add(profile)
        db.flush()
        db.add(AccreditationCriterion(
            tenant_id=None, profile_id=profile.id, code="C1", title="Consultants",
            metric="consultant_count", operator="gte", target_value=2,
        ))
        db.commit()

        listing = client.get(f"{API}/accreditation/profiles", headers=auth("hod@tth.health"))
        assert listing.status_code == 200
        assert "SHARED-STD" in {row["code"] for row in listing.json()}

        detail = client.get(f"{API}/accreditation/profiles/{profile.id}",
                            headers=auth("hod@tth.health"))
        assert detail.status_code == 200
        assert len(detail.json()["criteria"]) == 1

    def test_a_shared_profile_can_generate_a_return(
        self, client: TestClient, institution: dict, db: Session, auth
    ):
        """The end-to-end path that originally failed: a shared standard was invisible,
        so no department could produce an accreditation return at all."""
        profile = AccreditationProfile(
            tenant_id=None, body="mdcn", body_name="MDCN", code="SHARED-GEN",
            name="Shared", version="1.0",
        )
        db.add(profile)
        db.flush()
        db.add(AccreditationCriterion(
            tenant_id=None, profile_id=profile.id, code="I1", title="Theatres",
            metric="infrastructure", operator="gte", target_value=1,
            parameters={"capacity_key": "operating_theatres"},
        ))
        db.commit()

        response = client.post(
            f"{API}/accreditation/reviews"
            f"?org_unit_id={institution['department'].id}&profile_id={profile.id}&persist=false",
            headers=auth("hod@tth.health"),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["essential_total"] == 1
        assert body["criteria"][0]["measured"] == 2

    def test_shared_notification_template_is_resolved(self, db: Session, institution: dict):
        from app.services.notifications import resolve_template

        db.add(NotificationTemplate(
            tenant_id=None, event_code="custom.event", channel="in_app",
            subject="Shared subject", body="Shared body {{name}}",
        ))
        db.flush()

        subject, body = resolve_template(db, institution["tenant"].id, "custom.event", "in_app")
        assert subject == "Shared subject"
        assert body == "Shared body {{name}}"

    def test_institution_template_overrides_the_shared_one(
        self, db: Session, institution: dict
    ):
        from app.services.notifications import resolve_template

        db.add(NotificationTemplate(
            tenant_id=None, event_code="custom.event2", channel="in_app",
            subject="Platform default", body="Default",
        ))
        db.add(NotificationTemplate(
            tenant_id=institution["tenant"].id, event_code="custom.event2", channel="in_app",
            subject="Our wording", body="Local",
        ))
        db.flush()

        subject, _ = resolve_template(db, institution["tenant"].id, "custom.event2", "in_app")
        assert subject == "Our wording"

    def test_institution_wide_assessment_templates_are_offered_to_a_department(
        self, client: TestClient, institution: dict, auth
    ):
        """The fixture's Mini-CEX has a NULL org_unit_id, meaning institution-wide. It
        must appear when a department filters the instrument list."""
        response = client.get(
            f"{API}/assessments/templates",
            headers=auth("consultant@tth.health"),
            params={"org_unit_id": institution["department"].id},
        )
        assert response.status_code == 200
        assert "MINI-CEX" in {row["code"] for row in response.json()}
