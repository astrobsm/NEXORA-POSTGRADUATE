"""API contract and access-control tests.

The RBAC boundaries matter more than most: a residency platform holds assessments,
promotion decisions and de-identified clinical activity. These tests assert that a
trainee cannot read a peer's record and cannot sign off their own work.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.enums import ValidationStatus
from app.models.logbook import LogEntry
from tests.conftest import TEST_PASSWORD

API = "/api/v1"


class TestService:
    def test_health(self, client: TestClient):
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"

    def test_openapi_builds(self, client: TestClient):
        response = client.get(f"{API}/openapi.json")
        assert response.status_code == 200
        assert len(response.json()["paths"]) > 50

    def test_vocabularies_are_public(self, client: TestClient):
        """The sign-in screen and the offline shell need these before a session exists."""
        response = client.get(f"{API}/meta/vocabularies")
        assert response.status_code == 200
        body = response.json()
        assert "requirement_kinds" in body
        assert any(v["value"] == "procedure_count" for v in body["requirement_kinds"])

    def test_security_headers_present(self, client: TestClient):
        response = client.get("/health")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "X-Request-Id" in response.headers


class TestAuthentication:
    def test_login_and_me(self, client: TestClient, institution: dict):
        response = client.post(f"{API}/auth/login",
                               json={"email": "registrar@tth.health", "password": TEST_PASSWORD})
        assert response.status_code == 200
        tokens = response.json()
        assert tokens["token_type"] == "bearer"

        me = client.get(f"{API}/auth/me",
                        headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert me.status_code == 200
        body = me.json()
        assert body["user"]["email"] == "registrar@tth.health"
        assert body["enrolment"]["current_year"] == 2
        assert "logbook.entry.create" in body["permissions"]

    def test_wrong_password_is_indistinguishable_from_unknown_account(
        self, client: TestClient, institution: dict
    ):
        """Neither response may reveal whether the account exists."""
        wrong = client.post(f"{API}/auth/login",
                            json={"email": "registrar@tth.health", "password": "nope"})
        unknown = client.post(f"{API}/auth/login",
                              json={"email": "ghost@tth.health", "password": "nope"})
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json()["detail"] == unknown.json()["detail"]

    def test_unauthenticated_request_is_rejected(self, client: TestClient, institution: dict):
        assert client.get(f"{API}/logbook").status_code == 401

    def test_refresh_rotates_the_token(self, client: TestClient, institution: dict):
        login = client.post(f"{API}/auth/login",
                            json={"email": "registrar@tth.health", "password": TEST_PASSWORD})
        original = login.json()["refresh_token"]

        first = client.post(f"{API}/auth/refresh", json={"refresh_token": original})
        assert first.status_code == 200

        # The old refresh token must not work twice â€” replay protection.
        replay = client.post(f"{API}/auth/refresh", json={"refresh_token": original})
        assert replay.status_code == 401

    def test_password_policy_is_enforced_on_change(
        self, client: TestClient, institution: dict, auth
    ):
        headers = auth("registrar@tth.health")
        response = client.post(f"{API}/auth/password", headers=headers,
                               json={"current_password": TEST_PASSWORD, "new_password": "short"})
        assert response.status_code == 422
        assert "characters" in response.json()["detail"]


class TestLogbookAccessControl:
    def _create_entry(self, client: TestClient, headers: dict, **overrides) -> dict:
        payload = {
            "entry_type": "major_procedure",
            "occurred_at": datetime.combine(date.today() - timedelta(days=2),
                                            time(9, 0)).isoformat(),
            "title": "Appendicectomy",
            "participation_role": "performed_supervised",
            "procedure_grade": "major",
            **overrides,
        }
        response = client.post(f"{API}/logbook", headers=headers, json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    def test_entry_starts_pending_and_does_not_count(
        self, client: TestClient, institution: dict, auth
    ):
        entry = self._create_entry(client, auth("registrar@tth.health"))
        assert entry["validation_status"] == "pending"
        assert entry["validated_at"] is None

    def test_offline_replay_is_idempotent(self, client: TestClient, institution: dict, auth):
        """A device retrying after a dropped connection must not duplicate the record."""
        headers = auth("registrar@tth.health")
        first = self._create_entry(client, headers, client_uuid="device-uuid-1",
                                   captured_offline=True)
        second = self._create_entry(client, headers, client_uuid="device-uuid-1",
                                    captured_offline=True)
        assert first["id"] == second["id"]

    def test_trainee_cannot_read_a_peers_logbook(
        self, client: TestClient, institution: dict, auth
    ):
        owner = auth("registrar@tth.health")
        entry = self._create_entry(client, owner)

        peer = auth("junior@tth.health")
        response = client.get(f"{API}/logbook/{entry['id']}", headers=peer)
        assert response.status_code == 403

    def test_trainee_cannot_validate_their_own_entry(
        self, client: TestClient, institution: dict, auth
    ):
        headers = auth("registrar@tth.health")
        entry = self._create_entry(client, headers)
        response = client.post(f"{API}/logbook/{entry['id']}/validation", headers=headers,
                               json={"decision": "validated"})
        assert response.status_code == 403

    def test_supervisor_validates_and_entry_locks(
        self, client: TestClient, institution: dict, auth
    ):
        trainee = auth("registrar@tth.health")
        entry = self._create_entry(client, trainee)

        consultant = auth("consultant@tth.health")
        validated = client.post(f"{API}/logbook/{entry['id']}/validation", headers=consultant,
                                json={"decision": "validated", "comment": "Good technique."})
        assert validated.status_code == 200
        assert validated.json()["validation_status"] == "validated"

        # A validated entry is evidence; the trainee may no longer edit it.
        amend = client.patch(f"{API}/logbook/{entry['id']}", headers=trainee,
                             json={"title": "Something else"})
        assert amend.status_code == 409

    def test_query_requires_a_comment(self, client: TestClient, institution: dict, auth):
        entry = self._create_entry(client, auth("registrar@tth.health"))
        response = client.post(f"{API}/logbook/{entry['id']}/validation",
                               headers=auth("consultant@tth.health"),
                               json={"decision": "queried"})
        assert response.status_code == 422
        assert "comment is required" in response.json()["detail"]

    def test_queried_entry_returns_to_pending_after_amendment(
        self, client: TestClient, institution: dict, auth
    ):
        trainee = auth("registrar@tth.health")
        entry = self._create_entry(client, trainee)
        client.post(f"{API}/logbook/{entry['id']}/validation",
                    headers=auth("consultant@tth.health"),
                    json={"decision": "queried", "comment": "Which side?"})

        amended = client.patch(f"{API}/logbook/{entry['id']}", headers=trainee,
                               json={"title": "Right inguinal hernia repair"})
        assert amended.status_code == 200
        assert amended.json()["validation_status"] == "pending"

    def test_validated_entry_cannot_be_withdrawn(
        self, client: TestClient, institution: dict, auth
    ):
        trainee = auth("registrar@tth.health")
        entry = self._create_entry(client, trainee)
        client.post(f"{API}/logbook/{entry['id']}/validation",
                    headers=auth("consultant@tth.health"), json={"decision": "validated"})
        assert client.delete(f"{API}/logbook/{entry['id']}", headers=trainee).status_code == 409

    def test_history_records_every_transition(self, client: TestClient, institution: dict, auth):
        trainee = auth("registrar@tth.health")
        entry = self._create_entry(client, trainee)
        client.post(f"{API}/logbook/{entry['id']}/validation",
                    headers=auth("consultant@tth.health"), json={"decision": "validated"})
        history = client.get(f"{API}/logbook/{entry['id']}/history", headers=trainee)
        assert history.status_code == 200
        actions = [row["action"] for row in history.json()]
        assert actions == ["created", "validation"]


class TestPrivilegeEscalation:
    def test_trainee_cannot_list_users(self, client: TestClient, institution: dict, auth):
        assert client.get(f"{API}/users", headers=auth("registrar@tth.health")).status_code == 403

    def test_trainee_cannot_build_curriculum(self, client: TestClient, institution: dict, auth):
        response = client.post(
            f"{API}/curriculum/programmes", headers=auth("registrar@tth.health"),
            json={"org_unit_id": institution["department"].id, "code": "X", "name": "X"},
        )
        assert response.status_code == 403

    def test_head_of_department_lacks_role_administration(
        self, client: TestClient, institution: dict, auth
    ):
        """Role authoring is institution-level authority, not departmental."""
        response = client.post(
            f"{API}/users/roles", headers=auth("hod@tth.health"),
            json={"code": "anyrole", "name": "Any Role", "permission_codes": []},
        )
        assert response.status_code == 403
        assert "identity.role.manage" in response.json()["detail"]

    def test_role_creation_cannot_grant_unheld_permissions(
        self, client: TestClient, institution: dict, auth
    ):
        """The Director may author roles, but cannot invent authority they lack —
        otherwise role creation would be a privilege-escalation path."""
        response = client.post(
            f"{API}/users/roles", headers=auth("director@tth.health"),
            json={"code": "superrole", "name": "Super Role",
                  "permission_codes": ["platform.tenant.manage"]},
        )
        assert response.status_code == 403
        assert "do not hold" in response.json()["detail"]

    def test_role_creation_succeeds_within_held_authority(
        self, client: TestClient, institution: dict, auth
    ):
        response = client.post(
            f"{API}/users/roles", headers=auth("director@tth.health"),
            json={"code": "audit_lead", "name": "Clinical Audit Lead", "rank": 40,
                  "permission_codes": ["logbook.entry.read.any", "analytics.department.read"]},
        )
        assert response.status_code == 201, response.text
        assert response.json()["code"] == "audit_lead"

    def test_cannot_assign_a_role_more_senior_than_your_own(
        self, client: TestClient, institution: dict, auth
    ):
        """The Director of Residency (rank 20) may not appoint a CMD (rank 15)."""
        cmd_role = institution["roles"]["chief_medical_director"]
        response = client.post(
            f"{API}/users/roles/assign", headers=auth("director@tth.health"),
            json={"user_id": institution["registrar"].id, "role_id": cmd_role.id,
                  "org_unit_id": institution["department"].id},
        )
        assert response.status_code == 403
        assert "more senior" in response.json()["detail"]

    def test_can_assign_a_role_junior_to_your_own(
        self, client: TestClient, institution: dict, auth
    ):
        consultant_role = institution["roles"]["consultant"]
        response = client.post(
            f"{API}/users/roles/assign", headers=auth("director@tth.health"),
            json={"user_id": institution["junior"].id, "role_id": consultant_role.id,
                  "org_unit_id": institution["department"].id},
        )
        assert response.status_code == 200, response.text

    def test_assessor_cannot_assess_themselves(self, client: TestClient, institution: dict, auth):
        response = client.post(
            f"{API}/assessments", headers=auth("registrar@tth.health"),
            json={"template_id": institution["template"].id,
                  "enrolment_id": institution["enrolment"].id,
                  "occurred_on": str(date.today()), "responses": {}},
        )
        assert response.status_code in (403, 404)


class TestAssessmentScoring:
    def test_weighted_scoring_and_verdict(self, client: TestClient, institution: dict, auth):
        response = client.post(
            f"{API}/assessments", headers=auth("consultant@tth.health"),
            json={
                "template_id": institution["template"].id,
                "enrolment_id": institution["enrolment"].id,
                "occurred_on": str(date.today()),
                "responses": {"history": 9, "overall": 9, "comment": "Excellent."},
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        # history(9x1) + overall(9x2) = 27 of a possible 27 â†’ 100%
        assert body["percent_score"] == 100.0
        assert body["is_pass"] is True
        assert body["verdict"] == "outstanding"

    def test_not_applicable_fields_are_excluded_from_the_denominator(
        self, client: TestClient, institution: dict, auth
    ):
        response = client.post(
            f"{API}/assessments", headers=auth("consultant@tth.health"),
            json={
                "template_id": institution["template"].id,
                "enrolment_id": institution["enrolment"].id,
                "occurred_on": str(date.today()),
                "responses": {"history": None, "overall": 6},
            },
        )
        assert response.status_code == 201
        body = response.json()
        # Only 'overall' counts: 6 of 9 â†’ 66.7%
        assert body["max_score"] == 18.0
        assert round(body["percent_score"], 1) == 66.7


class TestSyncProtocol:
    def test_push_rejects_server_authoritative_collections(
        self, client: TestClient, institution: dict, auth
    ):
        response = client.post(
            f"{API}/sync/push", headers=auth("registrar@tth.health"),
            json={"device_id": "dev-1", "items": [
                {"collection": "enrolments", "op": "create", "data": {"current_year": 4}},
            ]},
        )
        assert response.status_code == 200
        assert response.json()["summary"]["rejected"] == 1
        assert "server-authoritative" in response.json()["rejected"][0]["reason"]

    def test_push_detects_a_stale_revision_as_a_conflict(
        self, client: TestClient, institution: dict, auth, db: Session
    ):
        headers = auth("registrar@tth.health")
        created = client.post(f"{API}/logbook", headers=headers, json={
            "entry_type": "admission",
            "occurred_at": datetime.combine(date.today(), time(9, 0)).isoformat(),
            "title": "Admission",
        })
        entry_id = created.json()["id"]
        current_revision = created.json()["revision"]

        # The server moves on (a supervisor validates it) while the device is offline.
        client.post(f"{API}/logbook/{entry_id}/validation",
                    headers=auth("consultant@tth.health"), json={"decision": "validated"})

        response = client.post(f"{API}/sync/push", headers=headers, json={
            "device_id": "dev-1",
            "items": [{"collection": "log_entries", "op": "update", "id": entry_id,
                       "base_revision": current_revision, "data": {"title": "Edited offline"}}],
        })
        assert response.status_code == 200
        assert response.json()["summary"]["conflicts"] == 1
        conflict = response.json()["conflicts"][0]
        assert conflict["server_revision"] > conflict["client_revision"]
        # The server row must be untouched.
        assert db.get(LogEntry, entry_id).title == "Admission"

    def test_pull_is_scoped_to_the_caller(self, client: TestClient, institution: dict, auth):
        response = client.get(f"{API}/sync/pull", headers=auth("registrar@tth.health"),
                              params={"device_id": "dev-1"})
        assert response.status_code == 200
        body = response.json()
        assert "log_entries" in body["data"]
        # The registrar's own enrolment is visible; nobody else's.
        assert all(e["trainee_id"] == institution["registrar"].id
                   for e in body["data"]["enrolments"])

    def test_pull_rejects_unknown_collections(self, client: TestClient, institution: dict, auth):
        response = client.get(f"{API}/sync/pull", headers=auth("registrar@tth.health"),
                              params={"device_id": "dev-1", "collections": ["patients"]})
        assert response.status_code == 422


class TestAcademicAttendance:
    def test_self_checkin_requires_the_session_code(
        self, client: TestClient, institution: dict, auth
    ):
        activity_id = institution["activity"].id
        bad = client.post(f"{API}/academic/activities/{activity_id}/attendance",
                          headers=auth("junior@tth.health"),
                          json={"checkin_code": "WRONG", "role": "attendee"})
        assert bad.status_code == 403

        good = client.post(f"{API}/academic/activities/{activity_id}/attendance",
                           headers=auth("junior@tth.health"),
                           json={"checkin_code": "ABC123", "role": "attendee"})
        assert good.status_code == 200
        assert good.json()["recorded"] == 1

    def test_attendance_awards_cme_credit(self, client: TestClient, institution: dict, auth):
        activity_id = institution["activity"].id
        client.post(f"{API}/academic/activities/{activity_id}/attendance",
                    headers=auth("junior@tth.health"),
                    json={"checkin_code": "ABC123", "role": "attendee"})
        ledger = client.get(f"{API}/academic/cme/ledger", headers=auth("junior@tth.health"))
        assert ledger.status_code == 200
        assert ledger.json()["total_credits"] == 2.0
