"""The requirement engine is the platform's core claim. These tests pin its behaviour."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.curriculum import RequirementRule
from app.models.enums import (
    CompetencyDomain,
    EntrustmentLevel,
    LogEntryType,
    ParticipationRole,
    RequirementKind,
    RequirementOperator,
    RequirementScope,
    RequirementSeverity,
    ValidationStatus,
)
from app.models.logbook import LogEntry
from app.services import requirements


def _log(db: Session, institution: dict, *, when: date, role: str, grade: str = "major",
         status: str = ValidationStatus.VALIDATED, quantity: int = 1,
         entry_type: str = LogEntryType.MAJOR_PROCEDURE) -> LogEntry:
    entry = LogEntry(
        tenant_id=institution["tenant"].id,
        enrolment_id=institution["enrolment"].id,
        org_unit_id=institution["department"].id,
        entry_type=entry_type,
        occurred_at=datetime.combine(when, time(10, 0)),
        occurred_on=when,
        title="Appendicectomy",
        procedure_id=institution["procedure"].id,
        procedure_name="Appendicectomy",
        procedure_grade=grade,
        participation_role=role,
        quantity=quantity,
        validation_status=status,
        supervisor_id=institution["consultant"].id,
    )
    db.add(entry)
    return entry


def _rule(institution: dict, **kwargs) -> RequirementRule:
    defaults = {
        "tenant_id": institution["tenant"].id,
        "curriculum_version_id": institution["version"].id,
        "label": "Test rule",
        "operator": RequirementOperator.GTE,
        "scope": RequirementScope.PROGRAMME,
        "severity": RequirementSeverity.MANDATORY,
    }
    return RequirementRule(**{**defaults, **kwargs})


def _ctx(db: Session, institution: dict) -> requirements.EvaluationContext:
    return requirements.EvaluationContext(
        db=db, enrolment=institution["enrolment"], as_of=date.today(), training_year=2
    )


# --------------------------------------------------------------------------
class TestProcedureCounting:
    def test_counts_only_validated_entries(self, db: Session, institution: dict):
        """Unvalidated activity must never count. This is the platform's central
        integrity guarantee: a trainee cannot inflate their record unilaterally."""
        base = date.today() - timedelta(days=30)
        for offset in range(5):
            _log(db, institution, when=base + timedelta(days=offset),
                 role=ParticipationRole.PERFORMED_SUPERVISED)
        for offset in range(3):
            _log(db, institution, when=base + timedelta(days=offset),
                 role=ParticipationRole.PERFORMED_SUPERVISED,
                 status=ValidationStatus.PENDING)
        db.flush()

        rule = _rule(institution, kind=RequirementKind.PROCEDURE_COUNT, target_value=5)
        result = requirements.evaluate_rule(_ctx(db, institution), rule)

        assert result.measured == 5
        assert result.met is True

    def test_role_filter_and_weighting(self, db: Session, institution: dict):
        base = date.today() - timedelta(days=20)
        _log(db, institution, when=base, role=ParticipationRole.OBSERVED)
        _log(db, institution, when=base, role=ParticipationRole.ASSISTED)
        _log(db, institution, when=base, role=ParticipationRole.PERFORMED_INDEPENDENT)
        db.flush()

        independent = _rule(
            institution, kind=RequirementKind.PROCEDURE_ROLE_COUNT, target_value=1,
            parameters={"role": ParticipationRole.PERFORMED_INDEPENDENT},
        )
        assert requirements.evaluate_rule(_ctx(db, institution), independent).measured == 1

        weighted = _rule(
            institution, kind=RequirementKind.PROCEDURE_ROLE_COUNT, target_value=1,
            parameters={"weighted": True},
        )
        # 0.25 (observed) + 0.5 (assisted) + 1.25 (independent) = 2.0
        assert requirements.evaluate_rule(_ctx(db, institution), weighted).measured == pytest.approx(2.0)

    def test_quantity_is_summed(self, db: Session, institution: dict):
        _log(db, institution, when=date.today() - timedelta(days=5),
             role=ParticipationRole.ASSISTED, quantity=4)
        db.flush()
        rule = _rule(institution, kind=RequirementKind.PROCEDURE_COUNT, target_value=4)
        assert requirements.evaluate_rule(_ctx(db, institution), rule).measured == 4


class TestOperatorsAndProgress:
    @pytest.mark.parametrize(
        ("operator", "target", "expected"),
        [
            (RequirementOperator.GTE, 3, True),
            (RequirementOperator.GTE, 4, False),
            (RequirementOperator.GT, 3, False),
            (RequirementOperator.LTE, 3, True),
            (RequirementOperator.LT, 3, False),
            (RequirementOperator.EQ, 3, True),
            (RequirementOperator.NEQ, 3, False),
        ],
    )
    def test_operators(self, db: Session, institution: dict, operator, target, expected):
        for offset in range(3):
            _log(db, institution, when=date.today() - timedelta(days=offset + 1),
                 role=ParticipationRole.ASSISTED)
        db.flush()
        rule = _rule(institution, kind=RequirementKind.PROCEDURE_COUNT,
                     operator=operator, target_value=target)
        assert requirements.evaluate_rule(_ctx(db, institution), rule).met is expected

    def test_progress_is_proportional_when_unmet(self, db: Session, institution: dict):
        for offset in range(5):
            _log(db, institution, when=date.today() - timedelta(days=offset + 1),
                 role=ParticipationRole.ASSISTED)
        db.flush()
        rule = _rule(institution, kind=RequirementKind.PROCEDURE_COUNT, target_value=20)
        result = requirements.evaluate_rule(_ctx(db, institution), rule)
        assert result.met is False
        assert result.progress_percent == pytest.approx(25.0)
        assert result.shortfall == pytest.approx(15.0)

    def test_progress_caps_at_100(self, db: Session, institution: dict):
        for offset in range(10):
            _log(db, institution, when=date.today() - timedelta(days=offset + 1),
                 role=ParticipationRole.ASSISTED)
        db.flush()
        rule = _rule(institution, kind=RequirementKind.PROCEDURE_COUNT, target_value=2)
        result = requirements.evaluate_rule(_ctx(db, institution), rule)
        assert result.progress_percent == 100.0
        assert result.shortfall == 0.0


class TestWindowing:
    def test_training_year_scope_excludes_other_years(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        # One entry in year 1, one in year 2.
        _log(db, institution, when=enrolment.start_date + timedelta(days=30),
             role=ParticipationRole.ASSISTED)
        _log(db, institution, when=enrolment.start_date + timedelta(days=400),
             role=ParticipationRole.ASSISTED)
        db.flush()

        year_one = _rule(institution, kind=RequirementKind.PROCEDURE_COUNT, target_value=1,
                         scope=RequirementScope.TRAINING_YEAR,
                         parameters={"training_year": 1})
        result = requirements.evaluate_rule(_ctx(db, institution), year_one)
        assert result.measured == 1
        assert result.detail["window"][0] == str(enrolment.start_date)

    def test_programme_scope_spans_the_whole_enrolment(self, db: Session, institution: dict):
        enrolment = institution["enrolment"]
        _log(db, institution, when=enrolment.start_date + timedelta(days=30),
             role=ParticipationRole.ASSISTED)
        _log(db, institution, when=enrolment.start_date + timedelta(days=400),
             role=ParticipationRole.ASSISTED)
        db.flush()
        rule = _rule(institution, kind=RequirementKind.PROCEDURE_COUNT, target_value=2)
        assert requirements.evaluate_rule(_ctx(db, institution), rule).measured == 2


class TestCompetencyLevels:
    def test_unrated_competency_scores_zero_not_pass(self, db: Session, institution: dict):
        """A competency nobody has assessed must not silently satisfy a requirement."""
        rule = _rule(
            institution, kind=RequirementKind.EPA_LEVEL, target_value=3,
            parameters={"epas_only": True, "aggregate": "min",
                        "level": EntrustmentLevel.INDIRECT_SUPERVISION},
        )
        result = requirements.evaluate_rule(_ctx(db, institution), rule)
        assert result.measured == 0
        assert result.met is False
        assert result.detail["competencies_unrated"] == 1

    def test_level_name_normalises_to_rank(self, db: Session, institution: dict):
        from app.models.assessment import CompetencyRating

        db.add(CompetencyRating(
            tenant_id=institution["tenant"].id, enrolment_id=institution["enrolment"].id,
            competency_id=institution["epa"].id, level=EntrustmentLevel.INDEPENDENT,
            level_value=4, rated_on=date.today() - timedelta(days=3),
        ))
        db.flush()

        rule = _rule(
            institution, kind=RequirementKind.EPA_LEVEL, target_value=0,
            parameters={"epas_only": True, "aggregate": "min",
                        "level": EntrustmentLevel.INDIRECT_SUPERVISION},
        )
        result = requirements.evaluate_rule(_ctx(db, institution), rule)
        assert result.target == 3          # resolved from the level name, not target_value
        assert result.measured == 4
        assert result.met is True

    def test_latest_rating_supersedes_earlier_ones(self, db: Session, institution: dict):
        from app.models.assessment import CompetencyRating

        for days_ago, value, level in [
            (60, 2, EntrustmentLevel.DIRECT_SUPERVISION),
            (10, 4, EntrustmentLevel.INDEPENDENT),
        ]:
            db.add(CompetencyRating(
                tenant_id=institution["tenant"].id, enrolment_id=institution["enrolment"].id,
                competency_id=institution["epa"].id, level=level, level_value=value,
                rated_on=date.today() - timedelta(days=days_ago),
            ))
        db.flush()

        rule = _rule(institution, kind=RequirementKind.EPA_LEVEL, target_value=4,
                     parameters={"epas_only": True, "aggregate": "min"})
        assert requirements.evaluate_rule(_ctx(db, institution), rule).measured == 4

    def test_self_ratings_are_excluded(self, db: Session, institution: dict):
        from app.models.assessment import CompetencyRating

        db.add(CompetencyRating(
            tenant_id=institution["tenant"].id, enrolment_id=institution["enrolment"].id,
            competency_id=institution["epa"].id, level=EntrustmentLevel.SUPERVISE_OTHERS,
            level_value=5, rated_on=date.today(), is_self_rating=True,
        ))
        db.flush()
        rule = _rule(institution, kind=RequirementKind.EPA_LEVEL, target_value=1,
                     parameters={"epas_only": True, "aggregate": "min"})
        assert requirements.evaluate_rule(_ctx(db, institution), rule).measured == 0


class TestAttendance:
    def test_attendance_percentage(self, db: Session, institution: dict):
        rule = _rule(
            institution, kind=RequirementKind.ACADEMIC_ATTENDANCE_PCT, target_value=75,
            parameters={"activity_kinds": ["grand_round"]},
        )
        result = requirements.evaluate_rule(_ctx(db, institution), rule)
        # One mandatory grand round exists and the registrar attended it.
        assert result.measured == 100.0
        assert result.detail["expected"] == 1
        assert result.detail["attended"] == 1

    def test_no_sessions_reports_zero_with_explanation(self, db: Session, institution: dict):
        rule = _rule(
            institution, kind=RequirementKind.ACADEMIC_ATTENDANCE_PCT, target_value=75,
            parameters={"activity_kinds": ["tumour_board"]},
        )
        result = requirements.evaluate_rule(_ctx(db, institution), rule)
        assert result.measured == 0.0
        assert "no qualifying sessions" in result.detail["note"]


class TestCustomExpression:
    def test_derived_ratio(self, db: Session, institution: dict):
        base = date.today() - timedelta(days=40)
        for offset in range(3):
            _log(db, institution, when=base + timedelta(days=offset),
                 role=ParticipationRole.PERFORMED_INDEPENDENT)
        for offset in range(7):
            _log(db, institution, when=base + timedelta(days=offset),
                 role=ParticipationRole.ASSISTED)
        db.flush()

        rule = _rule(
            institution, kind=RequirementKind.CUSTOM_EXPRESSION, target_value=25,
            parameters={
                "expression": "independent / total * 100",
                "inputs": {
                    "independent": {
                        "kind": RequirementKind.PROCEDURE_ROLE_COUNT,
                        "parameters": {"role": ParticipationRole.PERFORMED_INDEPENDENT},
                    },
                    "total": {"kind": RequirementKind.PROCEDURE_COUNT, "parameters": {}},
                },
            },
        )
        result = requirements.evaluate_rule(_ctx(db, institution), rule)
        assert result.measured == pytest.approx(30.0)
        assert result.met is True

    def test_division_by_zero_is_survivable(self, db: Session, institution: dict):
        rule = _rule(
            institution, kind=RequirementKind.CUSTOM_EXPRESSION, target_value=25,
            parameters={
                "expression": "a / b",
                "inputs": {
                    "a": {"kind": RequirementKind.PROCEDURE_COUNT, "parameters": {}},
                    "b": {"kind": RequirementKind.PROCEDURE_COUNT, "parameters": {}},
                },
            },
        )
        result = requirements.evaluate_rule(_ctx(db, institution), rule)
        assert result.measured == 0.0

    def test_expression_cannot_escape_the_sandbox(self, db: Session, institution: dict):
        """Institution-authored formulas must not become remote code execution."""
        for expression in (
            "__import__('os').system('echo pwned')",
            "().__class__.__bases__[0]",
            "[x for x in range(10)]",
            "open('/etc/passwd').read()",
        ):
            rule = _rule(
                institution, kind=RequirementKind.CUSTOM_EXPRESSION, target_value=1,
                parameters={"expression": expression, "inputs": {}},
            )
            result = requirements.evaluate_rule(_ctx(db, institution), rule)
            assert result.measured == 0.0
            assert "error" in result.detail

    def test_unknown_name_is_rejected(self, db: Session, institution: dict):
        rule = _rule(
            institution, kind=RequirementKind.CUSTOM_EXPRESSION, target_value=1,
            parameters={"expression": "mystery * 2", "inputs": {}},
        )
        result = requirements.evaluate_rule(_ctx(db, institution), rule)
        assert "Unknown input" in result.detail["error"]


class TestEngineContract:
    def test_every_declared_kind_has_a_measurer(self):
        """A requirement kind the UI can offer but the engine cannot measure would be a
        silent trap for institutions authoring policy."""
        missing = [k.value for k in RequirementKind if k.value not in requirements.MEASURERS]
        assert missing == []

    def test_unknown_kind_fails_loudly_rather_than_passing(self, db: Session, institution: dict):
        rule = _rule(institution, kind="not_a_real_kind", target_value=1)
        result = requirements.evaluate_rule(_ctx(db, institution), rule)
        assert result.met is False
        assert "No measurer registered" in result.detail["error"]

    def test_blocking_failures_filters_to_mandatory(self, db: Session, institution: dict):
        mandatory = _rule(institution, kind=RequirementKind.PROCEDURE_COUNT, target_value=99,
                          severity=RequirementSeverity.MANDATORY, label="Mandatory")
        advisory = _rule(institution, kind=RequirementKind.PROCEDURE_COUNT, target_value=99,
                         severity=RequirementSeverity.RECOMMENDED, label="Advisory")
        ctx = _ctx(db, institution)
        results = [requirements.evaluate_rule(ctx, mandatory),
                   requirements.evaluate_rule(ctx, advisory)]
        blocking = requirements.blocking_failures(results)
        assert [r.label for r in blocking] == ["Mandatory"]
