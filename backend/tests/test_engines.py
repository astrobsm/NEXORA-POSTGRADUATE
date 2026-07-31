"""Promotion, rotation, supervisor allocation, scoring and accreditation engines."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.assessment import CompetencyRating
from app.models.curriculum import RequirementRule
from app.models.enums import (
    EnrolmentStatus,
    EntrustmentLevel,
    LeaveType,
    LogEntryType,
    ParticipationRole,
    PromotionOutcome,
    RagStatus,
    RequirementKind,
    RequirementScope,
    RequirementSeverity,
    RotationStatus,
    ScoreDomain,
    ValidationStatus,
)
from app.models.logbook import LogEntry
from app.models.training import LeaveRecord
from app.services import accreditation, allocation, promotion, rotation, scoring


# ==========================================================================
class TestRotationEngine:
    def test_plan_lays_rotations_back_to_back(self, db: Session, institution: dict):
        planned = rotation.plan_schedule(db, institution["enrolment"], to_year=2)
        assert len(planned) == 4          # two rotations in each of two years

        for earlier, later in zip(planned, planned[1:], strict=False):
            assert later.start_date == earlier.end_date + timedelta(days=1)

    def test_plan_assigns_a_supervisor_with_reasoning(self, db: Session, institution: dict):
        planned = rotation.plan_schedule(db, institution["enrolment"], to_year=1)
        first = planned[0]
        assert first.supervisor_id is not None
        assert "components" in first.supervisor_rationale
        assert first.supervisor_rationale["reasons"]

    def test_missing_training_years_is_an_actionable_error(self, db: Session, institution: dict):
        for year in list(institution["version"].training_years):
            db.delete(year)
        db.flush()
        db.refresh(institution["version"])  # drop the cached relationship
        with pytest.raises(rotation.RotationPlanningError, match="no training years"):
            rotation.plan_schedule(db, institution["enrolment"])

    def test_materialise_then_replan_preserves_completed_rotations(
        self, db: Session, institution: dict
    ):
        enrolment = institution["enrolment"]
        created = rotation.materialise(
            db, enrolment, rotation.plan_schedule(db, enrolment, to_year=2)
        )
        db.flush()
        created[0].status = RotationStatus.COMPLETED
        db.flush()

        rotation.materialise(
            db, enrolment, rotation.plan_schedule(db, enrolment, to_year=2), replace=True
        )
        db.flush()
        db.refresh(enrolment)
        assert any(r.status == RotationStatus.COMPLETED for r in enrolment.rotations)

    def test_close_refuses_while_mandatory_requirements_are_unmet(
        self, db: Session, institution: dict
    ):
        enrolment = institution["enrolment"]
        created = rotation.materialise(
            db, enrolment, rotation.plan_schedule(db, enrolment, to_year=1)
        )
        db.flush()
        target = created[0]

        db.add(RequirementRule(
            tenant_id=institution["tenant"].id,
            curriculum_version_id=institution["version"].id,
            rotation_template_id=target.rotation_template_id,
            label="10 major procedures this rotation",
            kind=RequirementKind.PROCEDURE_COUNT, operator="gte", target_value=10,
            scope=RequirementScope.ROTATION, severity=RequirementSeverity.MANDATORY,
        ))
        db.flush()

        with pytest.raises(ValueError, match="mandatory rotation requirement"):
            rotation.close_rotation(db, target, closed_by_id=institution["consultant"].id)

        # A supervisor may still override, and the override is recorded.
        rotation.close_rotation(db, target, closed_by_id=institution["consultant"].id,
                                force=True, comment="Redeployed to COVID response.")
        assert target.status == RotationStatus.COMPLETED
        assert target.supervisor_comment.startswith("Redeployed")

    def test_extension_cascades_to_later_rotations(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        created = rotation.materialise(
            db, enrolment, rotation.plan_schedule(db, enrolment, to_year=2)
        )
        db.flush()
        first, second = created[0], created[1]
        original_second_start = second.start_date

        touched = rotation.extend_rotation(
            db, first, new_end_date=first.end_date + timedelta(days=28),
            reason="Sick leave during the posting",
        )
        db.flush()
        assert first.status == RotationStatus.EXTENDED
        assert second.start_date == original_second_start + timedelta(days=28)
        assert len(touched) == 4

    def test_extension_must_move_forward(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        created = rotation.materialise(
            db, enrolment, rotation.plan_schedule(db, enrolment, to_year=1)
        )
        db.flush()
        with pytest.raises(ValueError, match="later than"):
            rotation.extend_rotation(db, created[0],
                                     new_end_date=created[0].start_date, reason="x")

    def test_remedial_posting_links_back(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        created = rotation.materialise(
            db, enrolment, rotation.plan_schedule(db, enrolment, to_year=1)
        )
        db.flush()
        remedial = rotation.create_remedial(db, created[0], weeks=8,
                                            reason="Procedural minima not met")
        db.flush()
        assert remedial.is_remedial is True
        assert remedial.remediates_id == created[0].id
        assert (remedial.end_date - remedial.start_date).days == 8 * 7 - 1

    def test_extending_leave_shifts_the_training_clock(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        rotation.materialise(db, enrolment, rotation.plan_schedule(db, enrolment, to_year=2))
        db.flush()
        original_end = enrolment.expected_end_date

        leave = LeaveRecord(
            tenant_id=institution["tenant"].id, enrolment_id=enrolment.id,
            leave_type=LeaveType.MATERNITY,
            start_date=date.today() + timedelta(days=10),
            end_date=date.today() + timedelta(days=100),
            extends_training=True, status="approved",
        )
        db.add(leave)
        db.flush()

        rotation.apply_leave_interruption(db, leave)
        db.flush()
        assert enrolment.interruption_days == 91
        assert enrolment.expected_end_date == original_end + timedelta(days=91)

    def test_ordinary_leave_does_not_shift_the_clock(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        original_end = enrolment.expected_end_date
        leave = LeaveRecord(
            tenant_id=institution["tenant"].id, enrolment_id=enrolment.id,
            leave_type=LeaveType.ANNUAL, start_date=date.today(),
            end_date=date.today() + timedelta(days=14), extends_training=False,
            status="approved",
        )
        db.add(leave)
        db.flush()
        assert rotation.apply_leave_interruption(db, leave) == []
        assert enrolment.expected_end_date == original_end


# ==========================================================================
class TestSupervisorAllocation:
    def test_expertise_match_ranks_first(self, db: Session, institution: dict):
        ranked = allocation.rank_research_supervisors(
            db, tenant_id=institution["tenant"].id,
            org_unit_id=institution["department"].id,
            trainee_id=institution["registrar"].id,
            keywords=["laparoscopy", "trauma"],
        )
        assert ranked
        assert ranked[0].user_id == institution["consultant"].id
        assert ranked[0].components["expertise"] > 0
        assert any("Expertise match" in reason for reason in ranked[0].reasons)

    def test_conflict_of_interest_excludes_a_supervisor(self, db: Session, institution: dict):
        from app.models.identity import SupervisorProfile
        from sqlalchemy import select

        profile = db.execute(
            select(SupervisorProfile).where(
                SupervisorProfile.user_id == institution["consultant"].id
            )
        ).scalar_one()
        profile.conflicts_of_interest = [institution["registrar"].id]
        db.flush()

        ranked = allocation.rank_research_supervisors(
            db, tenant_id=institution["tenant"].id,
            org_unit_id=institution["department"].id,
            trainee_id=institution["registrar"].id, keywords=["laparoscopy"],
        )
        assert institution["consultant"].id not in {c.user_id for c in ranked}

        with_ineligible = allocation.rank_research_supervisors(
            db, tenant_id=institution["tenant"].id,
            org_unit_id=institution["department"].id,
            trainee_id=institution["registrar"].id, keywords=["laparoscopy"],
            include_ineligible=True,
        )
        excluded = next(c for c in with_ineligible
                        if c.user_id == institution["consultant"].id)
        assert "conflict of interest" in excluded.exclusion_reason.lower()

    def test_supervision_cap_is_respected(self, db: Session, institution: dict):
        from sqlalchemy import select

        from app.models.identity import SupervisorProfile
        from app.models.research import ProjectSupervision, ResearchProject

        profile = db.execute(
            select(SupervisorProfile).where(
                SupervisorProfile.user_id == institution["consultant"].id
            )
        ).scalar_one()
        profile.max_supervisees = 1
        db.flush()

        project = ResearchProject(
            tenant_id=institution["tenant"].id, org_unit_id=institution["department"].id,
            principal_investigator_id=institution["junior"].id, title="Existing project",
        )
        db.add(project)
        db.flush()
        db.add(ProjectSupervision(
            tenant_id=institution["tenant"].id, project_id=project.id,
            supervisor_id=institution["consultant"].id, assigned_on=date.today(),
        ))
        db.flush()

        ranked = allocation.rank_research_supervisors(
            db, tenant_id=institution["tenant"].id,
            org_unit_id=institution["department"].id,
            trainee_id=institution["registrar"].id, keywords=["laparoscopy"],
        )
        assert institution["consultant"].id not in {c.user_id for c in ranked}

    def test_assignment_records_its_reasoning(self, db: Session, institution: dict):
        from app.models.research import ResearchProject

        project = ResearchProject(
            tenant_id=institution["tenant"].id, org_unit_id=institution["department"].id,
            enrolment_id=institution["enrolment"].id,
            principal_investigator_id=institution["registrar"].id,
            title="Outcomes of laparoscopic appendicectomy",
            keywords=["laparoscopy", "outcomes"],
        )
        db.add(project)
        db.flush()

        supervision = allocation.assign_research_supervisor(db, project)
        db.flush()
        assert supervision.allocation_method == "automatic"
        assert supervision.allocation_score["chosen"]["user_id"] == supervision.supervisor_id
        assert "weights" in supervision.allocation_score

    def test_no_eligible_supervisor_raises_an_actionable_error(
        self, db: Session, institution: dict
    ):
        from sqlalchemy import select

        from app.models.identity import SupervisorProfile
        from app.models.research import ResearchProject

        for profile in db.execute(select(SupervisorProfile)).scalars():
            profile.accepting_new = False
        db.flush()

        project = ResearchProject(
            tenant_id=institution["tenant"].id, org_unit_id=institution["department"].id,
            principal_investigator_id=institution["registrar"].id, title="Orphan project",
        )
        db.add(project)
        db.flush()
        with pytest.raises(ValueError, match="No eligible research supervisor"):
            allocation.assign_research_supervisor(db, project)


# ==========================================================================
class TestScoring:
    def test_unassessed_domains_are_excluded_not_zeroed(self, db: Session, institution: dict):
        """A curriculum with no leadership requirement must not score every trainee
        zero for leadership — that would silently punish the whole cohort for a
        curriculum-authoring omission."""
        db.add(RequirementRule(
            tenant_id=institution["tenant"].id,
            curriculum_version_id=institution["version"].id,
            label="5 major procedures", kind=RequirementKind.PROCEDURE_COUNT,
            operator="gte", target_value=5, scope=RequirementScope.PROGRAMME,
            severity=RequirementSeverity.MANDATORY,
            score_domain=ScoreDomain.CLINICAL_COMPETENCY,
        ))
        db.flush()

        report = scoring.compute_scores(db, institution["enrolment"])
        assert ScoreDomain.LEADERSHIP in report.unassessed_domains
        assert report.domains[ScoreDomain.LEADERSHIP].rag == RagStatus.UNKNOWN
        # The overall score reflects only the assessed domains.
        assert report.effective_weight_base < 1.0

    def test_score_reflects_requirement_progress(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        db.add(RequirementRule(
            tenant_id=institution["tenant"].id,
            curriculum_version_id=institution["version"].id,
            label="10 major procedures", kind=RequirementKind.PROCEDURE_COUNT,
            operator="gte", target_value=10, scope=RequirementScope.PROGRAMME,
            severity=RequirementSeverity.MANDATORY,
            score_domain=ScoreDomain.CLINICAL_COMPETENCY,
        ))
        db.flush()

        empty = scoring.compute_scores(db, enrolment)
        baseline = empty.domain_score(ScoreDomain.CLINICAL_COMPETENCY)

        for offset in range(10):
            db.add(LogEntry(
                tenant_id=institution["tenant"].id, enrolment_id=enrolment.id,
                entry_type=LogEntryType.MAJOR_PROCEDURE,
                occurred_at=datetime.combine(date.today() - timedelta(days=offset + 1), time(9)),
                occurred_on=date.today() - timedelta(days=offset + 1),
                title="Appendicectomy", procedure_grade="major",
                participation_role=ParticipationRole.PERFORMED_SUPERVISED,
                validation_status=ValidationStatus.VALIDATED,
            ))
        db.flush()

        after = scoring.compute_scores(db, enrolment)
        assert after.domain_score(ScoreDomain.CLINICAL_COMPETENCY) > baseline

    def test_snapshot_is_persisted_and_denormalised(self, db: Session, institution: dict):
        report, snapshot = scoring.score_and_persist(db, institution["enrolment"])
        db.flush()
        assert snapshot.overall_score == pytest.approx(report.overall_score)
        assert institution["enrolment"].latest_rag == report.overall_rag
        assert institution["enrolment"].last_scored_at is not None

    def test_weights_are_normalised(self, db: Session, institution: dict):
        institution["version"].score_weights = {
            ScoreDomain.CLINICAL_COMPETENCY: 10, ScoreDomain.RESEARCH: 10,
        }
        db.flush()
        weights = scoring.resolve_weights(institution["version"])
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_professionalism_penalises_rejected_entries(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        for index in range(10):
            db.add(LogEntry(
                tenant_id=institution["tenant"].id, enrolment_id=enrolment.id,
                entry_type=LogEntryType.ADMISSION,
                occurred_at=datetime.combine(date.today() - timedelta(days=index + 1), time(9)),
                occurred_on=date.today() - timedelta(days=index + 1), title="Admission",
                validation_status=ValidationStatus.REJECTED if index < 5
                else ValidationStatus.VALIDATED,
            ))
        db.flush()
        report = scoring.compute_scores(db, enrolment)
        assert report.domain_score(ScoreDomain.PROFESSIONALISM) < 100
        assert report.metrics["logbook"]["rejected"] == 5


# ==========================================================================
class TestPromotionEngine:
    def _add_rule(self, db: Session, institution: dict, **kwargs):
        rule = RequirementRule(
            tenant_id=institution["tenant"].id,
            curriculum_version_id=institution["version"].id,
            scope=RequirementScope.PROMOTION,
            severity=RequirementSeverity.MANDATORY,
            operator="gte",
            **kwargs,
        )
        db.add(rule)
        db.flush()
        return rule

    def test_blocked_by_unmet_mandatory_requirement(self, db: Session, institution: dict):
        self._add_rule(db, institution, label="50 major procedures",
                       kind=RequirementKind.PROCEDURE_COUNT, target_value=50)
        assessment = promotion.assess(db, institution["enrolment"], include_scores=False)
        assert assessment.outcome == PromotionOutcome.NOT_RECOMMENDED
        assert len(assessment.blocking) == 1
        assert "50 major procedures" in assessment.rationale

    def test_blocked_by_open_rotations(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        rotation.materialise(db, enrolment, rotation.plan_schedule(db, enrolment, to_year=2))
        db.flush()
        assessment = promotion.assess(db, enrolment, include_scores=False)
        assert assessment.checks["rotations"]["passed"] is False
        assert assessment.outcome == PromotionOutcome.NOT_RECOMMENDED

    def test_recommended_when_every_gate_is_clear(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        # Long enough in post to clear the time gate for year 2.
        enrolment.start_date = date.today() - timedelta(days=365 * 2 + 30)
        rotation.materialise(db, enrolment, rotation.plan_schedule(db, enrolment, to_year=2))
        db.flush()
        for row in enrolment.rotations:
            row.status = RotationStatus.COMPLETED
        db.flush()

        assessment = promotion.assess(db, enrolment, include_scores=False)
        assert assessment.outcome == PromotionOutcome.RECOMMENDED
        assert assessment.readiness_percent == pytest.approx(100.0)
        assert assessment.to_level == "senior_registrar"

    def test_time_served_excludes_interruptions(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        enrolment.start_date = date.today() - timedelta(days=365 * 2)
        enrolment.interruption_days = 180
        db.flush()
        served = promotion.months_served(enrolment, date.today())
        assert served == pytest.approx(24 - 6, abs=1)

    def test_suspended_enrolment_cannot_be_promoted(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        enrolment.status = EnrolmentStatus.SUSPENDED
        db.flush()
        assessment = promotion.assess(db, enrolment, include_scores=False)
        assert assessment.checks["standing"]["passed"] is False
        assert "not active" in assessment.rationale

    def test_decision_contradicting_the_engine_requires_a_reason(
        self, db: Session, institution: dict
    ):
        enrolment = institution["enrolment"]
        self._add_rule(db, institution, label="Impossible", kind=RequirementKind.PROCEDURE_COUNT,
                       target_value=9999)
        assessment = promotion.assess(db, enrolment, include_scores=False)
        review = promotion.record_review(db, enrolment, assessment)
        db.flush()

        with pytest.raises(ValueError, match="override reason"):
            promotion.apply_decision(db, review, outcome=PromotionOutcome.APPROVED,
                                     decided_by_id=institution["hod"].id)

        promotion.apply_decision(
            db, review, outcome=PromotionOutcome.APPROVED, decided_by_id=institution["hod"].id,
            override_reason="Committee accepted external logbook evidence.",
        )
        db.flush()
        assert review.override_reason is not None

    def test_approval_advances_the_trainee(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        enrolment.start_date = date.today() - timedelta(days=365 * 2 + 30)
        rotation.materialise(db, enrolment, rotation.plan_schedule(db, enrolment, to_year=2))
        db.flush()
        for row in enrolment.rotations:
            row.status = RotationStatus.COMPLETED
        db.flush()

        assessment = promotion.assess(db, enrolment, include_scores=False)
        review = promotion.record_review(db, enrolment, assessment)
        db.flush()
        promotion.apply_decision(db, review, outcome=PromotionOutcome.APPROVED,
                                 decided_by_id=institution["hod"].id)
        db.flush()
        # Year 2 is the final year in the test curriculum, so this completes training.
        assert enrolment.status == EnrolmentStatus.COMPLETED

    def test_exam_eligibility_is_a_separate_gate(self, db: Session, institution: dict):
        db.add(RequirementRule(
            tenant_id=institution["tenant"].id,
            curriculum_version_id=institution["version"].id,
            label="Dissertation submitted", kind=RequirementKind.DISSERTATION_STAGE,
            operator="gte", target_value=0, parameters={"stage": "college_submission"},
            scope=RequirementScope.EXAM_ELIGIBILITY, severity=RequirementSeverity.MANDATORY,
        ))
        db.flush()
        result = promotion.exam_eligibility(db, institution["enrolment"])
        assert result["eligible"] is False
        assert result["awarding_body"] == "wacs"
        assert len(result["blocking"]) == 1


# ==========================================================================
class TestAccreditation:
    def _profile(self, db: Session, institution: dict, criteria: list[dict]):
        from app.models.analytics import AccreditationCriterion, AccreditationProfile

        profile = AccreditationProfile(
            tenant_id=institution["tenant"].id, body="npmcn", body_name="NPMCN",
            code="TEST-ACC", name="Test Standard", version="1.0",
        )
        db.add(profile)
        db.flush()
        for index, criterion in enumerate(criteria):
            db.add(AccreditationCriterion(
                tenant_id=institution["tenant"].id, profile_id=profile.id,
                code=criterion.get("code", f"C{index}"), title=criterion["title"],
                metric=criterion["metric"], operator=criterion.get("operator", "gte"),
                target_value=criterion["target_value"],
                parameters=criterion.get("parameters", {}),
                weighting=criterion.get("weighting", "essential"), sort_order=index,
            ))
        db.flush()
        db.refresh(profile)
        return profile

    def test_infrastructure_reads_declared_capacity(self, db: Session, institution: dict):
        profile = self._profile(db, institution, [
            {"title": "Operating theatres", "metric": "infrastructure", "target_value": 2,
             "parameters": {"capacity_key": "operating_theatres"}},
        ])
        review, results = accreditation.generate_review(
            db, org_unit=institution["department"], profile=profile,
            period_start=date.today() - timedelta(days=365), period_end=date.today(),
            persist=False,
        )
        assert results[0].measured == 2
        assert results[0].met is True

    def test_unmet_criteria_become_ranked_gaps_with_a_narrative(
        self, db: Session, institution: dict
    ):
        profile = self._profile(db, institution, [
            {"code": "F1", "title": "Consultants", "metric": "consultant_count",
             "target_value": 10},
            {"code": "C1", "title": "Major operations", "metric": "annual_major_operations",
             "target_value": 400},
            {"code": "D1", "title": "Nice to have", "metric": "publication_count",
             "target_value": 5, "weighting": "desirable"},
        ])
        review, _ = accreditation.generate_review(
            db, org_unit=institution["department"], profile=profile,
            period_start=date.today() - timedelta(days=365), period_end=date.today(),
            persist=False,
        )
        assert review.readiness_rag == RagStatus.RED
        assert review.essential_total == 2
        # Essential gaps are ranked above desirable ones.
        assert review.gaps[0]["weighting"] == "essential"
        assert "essential criteria are met" in review.narrative
        assert "F1" in review.narrative

    def test_ratio_criterion_uses_the_right_direction(self, db: Session, institution: dict):
        """A trainer:trainee ratio is a ceiling, not a floor — the operator must be
        honoured or the verdict inverts."""
        profile = self._profile(db, institution, [
            {"title": "Trainer ratio", "metric": "trainer_trainee_ratio",
             "operator": "lte", "target_value": 4},
        ])
        _, results = accreditation.generate_review(
            db, org_unit=institution["department"], profile=profile,
            period_start=date.today() - timedelta(days=365), period_end=date.today(),
            persist=False,
        )
        # One enrolment against several supervisor-capable staff — comfortably under.
        assert results[0].met is True

    def test_unknown_metric_fails_the_criterion_rather_than_the_report(
        self, db: Session, institution: dict
    ):
        profile = self._profile(db, institution, [
            {"title": "Made up", "metric": "does_not_exist", "target_value": 1},
        ])
        _, results = accreditation.generate_review(
            db, org_unit=institution["department"], profile=profile,
            period_start=date.today() - timedelta(days=365), period_end=date.today(),
            persist=False,
        )
        assert results[0].met is False
        assert "Unknown metric" in results[0].detail["error"]

    def test_subtree_scoping_includes_child_units(self, db: Session, institution: dict):
        from app.models.tenancy import OrgUnit

        child = OrgUnit(
            tenant_id=institution["tenant"].id, parent_id=institution["department"].id,
            kind="unit", name="Trauma Unit", code="TTH-TRAUMA",
            path=f"{institution['department'].path}/TTH-TRAUMA", depth=2,
            capacity={"operating_theatres": 3},
        )
        db.add(child)
        db.flush()

        profile = self._profile(db, institution, [
            {"title": "Theatres", "metric": "infrastructure", "target_value": 5,
             "parameters": {"capacity_key": "operating_theatres"}},
        ])
        _, results = accreditation.generate_review(
            db, org_unit=institution["department"], profile=profile,
            period_start=date.today() - timedelta(days=365), period_end=date.today(),
            persist=False,
        )
        # 2 (department) + 3 (child unit)
        assert results[0].measured == 5
