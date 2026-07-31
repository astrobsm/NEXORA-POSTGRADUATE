"""Reading engagement, the Examination Readiness Score, and remediation.

The specification fixes the readiness weights and category boundaries exactly,
so those are asserted as literals rather than derived — if someone changes a
weight, this file should be the thing that objects.

The behavioural tests concentrate on the two decisions that make the score
defensible: unassessed components are excluded rather than zeroed, and the
confidence interval widens when evidence is thin.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.cme import CmeAssignment, CmeResource
from app.models.enums import (
    CitationStyle,
    CmeStatus,
    EditorialStatus,
    ReadinessCategory,
    ReadingEventKind,
)
from app.services import readiness, reading, remediation
from app.services.ai.cme_author import (
    ARTICLE_SECTIONS,
    REQUIRED_SECTIONS,
    ArticleError,
    Reference,
    build_article,
)


def make_resource(
    db: Session, institution: dict, *, sections: int = 4, words: int = 800, **kw
) -> CmeResource:
    resource = CmeResource(
        tenant_id=institution["tenant"].id,
        title=kw.pop("title", "Management of acute biliary disease"),
        topics=kw.pop("topics", ["biliary"]),
        estimated_minutes=kw.pop("estimated_minutes", 20),
        word_count=words,
        editorial_status=kw.pop("editorial_status", EditorialStatus.PUBLISHED),
        sections=[
            {"key": f"s{i}", "title": f"Section {i}", "order": i, "body": "x"}
            for i in range(sections)
        ],
        **kw,
    )
    db.add(resource)
    db.flush()
    return resource


def read_fully(
    db: Session, institution: dict, resource: CmeResource, *, when=None, seconds: int = 600
):
    """A complete, credible reading session."""
    session = reading.open_session(
        db,
        tenant_id=institution["tenant"].id,
        user_id=institution["registrar"].id,
        resource=resource,
        now=when,
    )
    for _ in range(seconds // 60):
        reading.record_event(
            db, session, kind=ReadingEventKind.HEARTBEAT, delta_seconds=60, occurred_at=when
        )
    for i in range(len(resource.sections)):
        reading.record_event(
            db,
            session,
            kind=ReadingEventKind.SECTION_COMPLETED,
            section_ref=f"s{i}",
            scroll_percent=100.0,
            occurred_at=when,
        )
    return session


# ==========================================================================
# Reading
# ==========================================================================
class TestReadingCapture:
    def test_an_unpublished_article_cannot_be_read_for_credit(
        self, db: Session, institution: dict
    ) -> None:
        resource = make_resource(
            db, institution, editorial_status=EditorialStatus.AI_DRAFT
        )
        with pytest.raises(reading.ReadingError, match="not published"):
            reading.open_session(
                db,
                tenant_id=institution["tenant"].id,
                user_id=institution["registrar"].id,
                resource=resource,
            )

    def test_a_second_visit_is_marked_as_a_revisit(
        self, db: Session, institution: dict
    ) -> None:
        resource = make_resource(db, institution)
        first = read_fully(db, institution, resource)
        second = reading.open_session(
            db,
            tenant_id=institution["tenant"].id,
            user_id=institution["registrar"].id,
            resource=resource,
        )
        assert first.is_revisit is False
        assert second.is_revisit is True

    def test_an_implausible_heartbeat_gap_is_not_credited(
        self, db: Session, institution: dict
    ) -> None:
        """A tab restored after two hours reports a huge delta.

        Crediting it would make the reading score a measure of browser tab
        hygiene rather than of reading.
        """
        resource = make_resource(db, institution)
        session = reading.open_session(
            db,
            tenant_id=institution["tenant"].id,
            user_id=institution["registrar"].id,
            resource=resource,
        )
        reading.record_event(
            db, session, kind=ReadingEventKind.HEARTBEAT, delta_seconds=7200
        )
        assert session.active_seconds == reading.MAX_HEARTBEAT_GAP_SECONDS

    def test_the_rollup_can_be_rebuilt_from_the_event_stream(
        self, db: Session, institution: dict
    ) -> None:
        """The reconciliation path for offline sync."""
        resource = make_resource(db, institution)
        session = read_fully(db, institution, resource)
        original = session.active_seconds

        session.active_seconds = 999_999
        session.sections_completed = []
        db.flush()

        reading.recompute_session(db, session)
        assert session.active_seconds == original
        assert len(session.sections_completed) == len(resource.sections)


class TestReadingCompletion:
    def test_a_glance_scores_nothing_however_far_it_scrolled(
        self, db: Session, institution: dict
    ) -> None:
        """Scroll-to-bottom in four seconds is not reading."""
        resource = make_resource(db, institution, sections=0)
        session = reading.open_session(
            db,
            tenant_id=institution["tenant"].id,
            user_id=institution["registrar"].id,
            resource=resource,
        )
        reading.record_event(
            db,
            session,
            kind=ReadingEventKind.SCROLLED,
            scroll_percent=100.0,
            delta_seconds=4,
        )
        assert reading.session_completion(session, resource) == 0.0

    def test_a_credible_full_read_scores_fully(
        self, db: Session, institution: dict
    ) -> None:
        resource = make_resource(db, institution, words=800)
        session = read_fully(db, institution, resource, seconds=600)
        assert reading.session_completion(session, resource) == pytest.approx(100.0)
        assert reading.is_complete(session, resource)

    def test_reading_faster_than_humanly_possible_is_discounted(
        self, db: Session, institution: dict
    ) -> None:
        """Caps the credit; never surfaced as an accusation."""
        resource = make_resource(db, institution, words=20_000)
        session = read_fully(db, institution, resource, seconds=60)
        completion = reading.session_completion(session, resource)
        assert 0 < completion < 100


class TestReadingScores:
    def test_no_assigned_reading_does_not_score_zero(
        self, db: Session, institution: dict
    ) -> None:
        """A trainee must not be penalised for an administrative omission.

        With nothing assigned, self-directed reading carries the whole score —
        the same principle the readiness engine applies to unassessed
        components.
        """
        resource = make_resource(db, institution)
        read_fully(db, institution, resource)
        scores = reading.compute_scores(
            db,
            user_id=institution["registrar"].id,
            window_start=date.today() - timedelta(days=30),
            window_end=date.today(),
        )
        assert scores.reading > 0
        assert scores.components["reading"]["basis"] == "self_directed_only"

    def test_completing_assigned_reading_scores_highly(
        self, db: Session, institution: dict
    ) -> None:
        resource = make_resource(db, institution)
        db.add(
            CmeAssignment(
                tenant_id=institution["tenant"].id,
                user_id=institution["registrar"].id,
                resource_id=resource.id,
                assigned_on=date.today() - timedelta(days=7),
                due_on=date.today() + timedelta(days=7),
                status=CmeStatus.COMPLETED,
            )
        )
        db.flush()
        read_fully(db, institution, resource)

        scores = reading.compute_scores(
            db,
            user_id=institution["registrar"].id,
            window_start=date.today() - timedelta(days=30),
            window_end=date.today(),
        )
        assert scores.reading >= 80

    def test_cramming_scores_worse_than_spreading(
        self, db: Session, institution: dict
    ) -> None:
        """The whole point of a consistency score.

        Same total reading, different distribution. The trainee who read on
        nine days has learned more than the one who read for six hours the
        night before, and the score has to say so.
        """
        end = date.today()
        start = end - timedelta(days=13)

        crammer = make_resource(db, institution, title="Crammed reading")
        for _ in range(6):
            read_fully(db, institution, crammer, when=utcnow(), seconds=600)
        crammed = reading.compute_scores(
            db, user_id=institution["registrar"].id, window_start=start, window_end=end
        )

        # Fresh window for the spread reader, to keep the two measurements apart.
        spread_user = institution["junior"].id
        spread = make_resource(db, institution, title="Spread reading")
        for day in range(6):
            when = utcnow() - timedelta(days=day * 2)
            session = reading.open_session(
                db,
                tenant_id=institution["tenant"].id,
                user_id=spread_user,
                resource=spread,
                now=when,
            )
            for _ in range(10):
                reading.record_event(
                    db,
                    session,
                    kind=ReadingEventKind.HEARTBEAT,
                    delta_seconds=60,
                    occurred_at=when,
                )
        spread_scores = reading.compute_scores(
            db, user_id=spread_user, window_start=start, window_end=end
        )

        assert spread_scores.consistency > crammed.consistency

    def test_a_single_day_of_reading_scores_zero_consistency(
        self, db: Session, institution: dict
    ) -> None:
        resource = make_resource(db, institution)
        read_fully(db, institution, resource)
        scores = reading.compute_scores(
            db,
            user_id=institution["registrar"].id,
            window_start=date.today() - timedelta(days=30),
            window_end=date.today(),
        )
        assert scores.consistency == 0.0
        assert scores.components["consistency"]["basis"] == "single_day"

    def test_engagement_signals_saturate(self, db: Session, institution: dict) -> None:
        """A hundred highlights on one paragraph must not outrank real interaction."""
        resource = make_resource(db, institution)
        session = read_fully(db, institution, resource)
        for _ in range(100):
            reading.record_event(db, session, kind=ReadingEventKind.HIGHLIGHTED)

        scores = reading.compute_scores(
            db,
            user_id=institution["registrar"].id,
            window_start=date.today() - timedelta(days=30),
            window_end=date.today(),
        )
        highlights = scores.components["engagement"]["highlights"]
        assert highlights["count"] == 100
        # Capped at its weight: 25 points, not 100 highlights' worth.
        assert highlights["contribution"] == pytest.approx(25.0)
        assert scores.engagement < 100

    def test_retention_is_null_not_zero_without_evidence(
        self, db: Session, institution: dict
    ) -> None:
        """A trainee in their first fortnight has no retention to measure."""
        resource = make_resource(db, institution)
        read_fully(db, institution, resource)
        scores = reading.compute_scores(
            db,
            user_id=institution["registrar"].id,
            window_start=date.today() - timedelta(days=30),
            window_end=date.today(),
        )
        assert scores.retention is None

    def test_a_snapshot_is_stored_once_per_window(
        self, db: Session, institution: dict
    ) -> None:
        resource = make_resource(db, institution)
        read_fully(db, institution, resource)
        args = {
            "tenant_id": institution["tenant"].id,
            "user_id": institution["registrar"].id,
            "window_start": date.today() - timedelta(days=30),
            "window_end": date.today(),
        }
        first = reading.snapshot_scores(db, **args)
        second = reading.snapshot_scores(db, **args)
        assert first.id == second.id


# ==========================================================================
# Readiness
# ==========================================================================
class TestReadinessWeights:
    def test_the_weights_are_exactly_as_specified(self) -> None:
        """Fixed by the curriculum specification, asserted as literals."""
        assert readiness.DEFAULT_WEIGHTS == {
            "cme_reading_completion": 0.20,
            "cbt_performance": 0.35,
            "procedural_logbook": 0.15,
            "clinical_competency": 0.10,
            "seminar_participation": 0.05,
            "journal_club_participation": 0.05,
            "case_presentations": 0.05,
            "professionalism_evaluation": 0.05,
        }

    def test_the_weights_sum_to_one(self) -> None:
        assert sum(readiness.DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)

    @pytest.mark.parametrize(
        ("score", "category"),
        [
            (100.0, ReadinessCategory.OUTSTANDING),
            (90.0, ReadinessCategory.OUTSTANDING),
            (89.9, ReadinessCategory.EXAMINATION_READY),
            (80.0, ReadinessCategory.EXAMINATION_READY),
            (79.9, ReadinessCategory.NEARLY_READY),
            (70.0, ReadinessCategory.NEARLY_READY),
            (69.9, ReadinessCategory.NEEDS_IMPROVEMENT),
            (60.0, ReadinessCategory.NEEDS_IMPROVEMENT),
            (59.9, ReadinessCategory.INTENSIVE_REMEDIATION),
            (0.0, ReadinessCategory.INTENSIVE_REMEDIATION),
        ],
    )
    def test_category_boundaries_are_exactly_as_specified(
        self, score: float, category: str
    ) -> None:
        assert readiness.category_for(score) == category


class TestReadinessAggregation:
    def test_unassessed_components_are_excluded_not_zeroed(
        self, db: Session, institution: dict
    ) -> None:
        """The central fairness property of the whole score.

        A department that has not yet run a journal club has produced no
        evidence about its trainees' journal-club participation. Recording that
        as 0/100 would penalise every one of them for an administrative gap.
        """
        result = readiness.compute_readiness(db, user_id=institution["registrar"].id)
        unassessed = result.unassessed_keys
        assert unassessed, "this fixture has sparse evidence, so some must be unassessed"
        for key in unassessed:
            assert result.as_component_dict()[key]["score"] is None
            assert result.as_component_dict()[key]["assessed"] is False
        # The score reflects only what was measured.
        assert 0 <= result.score <= 100

    def test_evidence_coverage_reports_how_much_is_actually_measured(
        self, db: Session, institution: dict
    ) -> None:
        result = readiness.compute_readiness(db, user_id=institution["registrar"].id)
        assert 0.0 <= result.evidence_coverage <= 1.0
        declared = sum(readiness.DEFAULT_WEIGHTS.values())
        assessed = sum(c.weight for c in result.assessed_components)
        assert result.evidence_coverage == pytest.approx(assessed / declared, abs=1e-4)

    def test_thin_evidence_widens_the_confidence_interval(
        self, db: Session, institution: dict
    ) -> None:
        """A point estimate on two data points would overstate what is known."""
        result = readiness.compute_readiness(db, user_id=institution["registrar"].id)
        width = result.confidence_high - result.confidence_low
        assert width > 0
        assert result.confidence_low <= result.score <= result.confidence_high

    def test_the_interval_stays_inside_the_scale(
        self, db: Session, institution: dict
    ) -> None:
        result = readiness.compute_readiness(db, user_id=institution["registrar"].id)
        assert result.confidence_low >= 0.0
        assert result.confidence_high <= 100.0

    def test_no_evidence_at_all_is_not_a_crash(
        self, db: Session, institution: dict
    ) -> None:
        result = readiness.compute_readiness(db, user_id=institution["junior"].id)
        assert result.score >= 0
        assert result.category in {c for _, c in readiness.CATEGORY_FLOORS}

    def test_all_nine_indices_are_reported(
        self, db: Session, institution: dict
    ) -> None:
        result = readiness.compute_readiness(db, user_id=institution["registrar"].id)
        assert set(result.indices) == {
            "knowledge",
            "clinical_competency",
            "procedural_competency",
            "critical_thinking",
            "consistency",
            "improvement_rate",
            "learning_velocity",
            "retention",
            "examination_prediction",
        }

    def test_the_weight_table_used_is_recorded(
        self, db: Session, institution: dict
    ) -> None:
        """A score computed today must still be explicable after a reweighting."""
        result = readiness.compute_readiness(db, user_id=institution["registrar"].id)
        assert result.weights_used == readiness.DEFAULT_WEIGHTS

    def test_a_custom_weight_table_is_honoured(
        self, db: Session, institution: dict
    ) -> None:
        """Policy is data: an institution may reweight without a deployment."""
        custom = dict(readiness.DEFAULT_WEIGHTS)
        custom["cbt_performance"] = 0.50
        custom["cme_reading_completion"] = 0.05
        result = readiness.compute_readiness(
            db, user_id=institution["registrar"].id, weights=custom
        )
        assert result.weights_used["cbt_performance"] == 0.50


class TestInfluentialFactors:
    def test_factors_are_ranked_by_counterfactual_gain(
        self, db: Session, institution: dict
    ) -> None:
        """Not by raw weight, which would rank identically for everyone."""
        result = readiness.compute_readiness(db, user_id=institution["registrar"].id)
        assessed = [f for f in result.influential_factors if f["status"] == "assessed"]
        gains = [f["readiness_gain_if_improved"] for f in assessed]
        assert gains == sorted(gains, reverse=True)

    def test_assessed_factors_outrank_unassessed_ones(
        self, db: Session, institution: dict
    ) -> None:
        """A plan should lead with what is measurably failing."""
        result = readiness.compute_readiness(db, user_id=institution["registrar"].id)
        statuses = [f["status"] for f in result.influential_factors]
        if "assessed" in statuses and "unassessed" in statuses:
            assert statuses.index("assessed") < statuses.index("unassessed")

    def test_every_factor_carries_an_actionable_instruction(
        self, db: Session, institution: dict
    ) -> None:
        result = readiness.compute_readiness(db, user_id=institution["registrar"].id)
        for factor in result.influential_factors:
            assert factor["action"]
            assert factor["label"]

    def test_a_snapshot_records_its_delta(self, db: Session, institution: dict) -> None:
        first = readiness.snapshot_readiness(
            db,
            tenant_id=institution["tenant"].id,
            user_id=institution["registrar"].id,
            as_of=date.today() - timedelta(days=30),
        )
        second = readiness.snapshot_readiness(
            db,
            tenant_id=institution["tenant"].id,
            user_id=institution["registrar"].id,
            as_of=date.today(),
        )
        assert first.delta_from_previous is None
        assert second.delta_from_previous == pytest.approx(second.score - first.score)


# ==========================================================================
# Remediation
# ==========================================================================
class TestRemediation:
    def test_a_plan_is_bounded_to_three_target_areas(
        self, db: Session, institution: dict
    ) -> None:
        """A plan for nine areas is a plan nobody follows."""
        snapshot = readiness.snapshot_readiness(
            db, tenant_id=institution["tenant"].id, user_id=institution["registrar"].id
        )
        plan = remediation.plan_from_readiness(db, snapshot)
        targeted = [a for a in plan.target_areas if not a.get("deferred")]
        assert len(targeted) <= remediation.MAX_TARGET_AREAS

    def test_deferred_areas_are_recorded_for_the_next_cycle(
        self, db: Session, institution: dict
    ) -> None:
        snapshot = readiness.snapshot_readiness(
            db, tenant_id=institution["tenant"].id, user_id=institution["registrar"].id
        )
        plan = remediation.plan_from_readiness(db, snapshot)
        deferred = [a for a in plan.target_areas if a.get("deferred")]
        if deferred:
            assert "deferred to the next cycle" in plan.rationale

    def test_actions_stay_inside_the_effort_budget(
        self, db: Session, institution: dict
    ) -> None:
        snapshot = readiness.snapshot_readiness(
            db, tenant_id=institution["tenant"].id, user_id=institution["registrar"].id
        )
        plan = remediation.plan_from_readiness(
            db, snapshot, weeks=2, weekly_budget_minutes=60
        )
        total = sum(a.estimated_minutes for a in plan.actions)
        assert total <= 2 * 60

    def test_every_target_area_carries_its_evidence(
        self, db: Session, institution: dict
    ) -> None:
        """A trainee must be able to see why they were asked to do something."""
        snapshot = readiness.snapshot_readiness(
            db, tenant_id=institution["tenant"].id, user_id=institution["registrar"].id
        )
        plan = remediation.plan_from_readiness(db, snapshot)
        for area in plan.target_areas:
            assert area["evidence"]
            assert area["evidence"]["source"]

    def test_a_missing_library_topic_produces_an_honest_action(
        self, db: Session, institution: dict
    ) -> None:
        """Better to name the gap than to emit a vague instruction."""
        areas = [
            remediation.WeakArea(
                key="a_topic_with_no_material",
                label="A Topic With No Material",
                kind="topic",
                score=35.0,
                priority=-100,
                evidence={"source": "test"},
            )
        ]
        plan = remediation.build_plan(
            db,
            tenant_id=institution["tenant"].id,
            user_id=institution["registrar"].id,
            areas=areas,
        )
        assert plan.actions
        assert any("No published article" in (a.detail or "") for a in plan.actions)

    def test_material_gaps_are_reported(self, db: Session, institution: dict) -> None:
        """The input a department needs before authorising generation spend."""
        areas = [
            remediation.WeakArea(
                key="uncovered_topic",
                label="Uncovered Topic",
                kind="topic",
                score=30.0,
                priority=-100,
                evidence={"source": "test"},
            )
        ]
        plan = remediation.build_plan(
            db,
            tenant_id=institution["tenant"].id,
            user_id=institution["registrar"].id,
            areas=areas,
        )
        assert remediation.topics_needing_material(db, plan) == ["uncovered_topic"]

    def test_no_weakness_produces_no_plan(self, db: Session, institution: dict) -> None:
        """A trainee who did well should not be handed a remediation plan."""
        from app.models.cbt import ExamAttempt
        from app.models.enums import AttemptStatus

        attempt = ExamAttempt(
            tenant_id=institution["tenant"].id,
            paper_id=None,
            user_id=institution["registrar"].id,
            started_at=utcnow(),
            status=AttemptStatus.MARKED,
            topic_breakdown={
                "biliary": {"served": 10, "correct": 9, "marks": 9, "available": 10}
            },
        )
        assert remediation.weak_areas_from_attempt(attempt) == []

    def test_a_thin_topic_score_is_ignored(self, db: Session, institution: dict) -> None:
        """One wrong answer out of one is a 0% topic and means nothing."""
        from app.models.cbt import ExamAttempt
        from app.models.enums import AttemptStatus

        attempt = ExamAttempt(
            tenant_id=institution["tenant"].id,
            user_id=institution["registrar"].id,
            started_at=utcnow(),
            status=AttemptStatus.MARKED,
            topic_breakdown={
                "rare": {"served": 1, "correct": 0, "marks": 0, "available": 1},
                "common": {"served": 10, "correct": 2, "marks": 2, "available": 10},
            },
        )
        keys = [a.key for a in remediation.weak_areas_from_attempt(attempt)]
        assert keys == ["common"]


# ==========================================================================
# CME article authoring
# ==========================================================================
class TestArticleStructure:
    def test_the_prescribed_sections_are_all_present_and_ordered(self) -> None:
        keys = [k for k, _ in ARTICLE_SECTIONS]
        assert keys[0] == "learning_objectives"
        assert keys[-1] == "references"
        for expected in (
            "anatomy",
            "physiology",
            "embryology",
            "histology",
            "pathology",
            "microbiology",
            "pharmacology",
            "clinical_features",
            "investigations",
            "differential_diagnosis",
            "management",
            "complications",
            "operative_techniques",
            "postoperative_care",
            "guidelines",
            "current_evidence",
            "landmark_trials",
            "recent_updates",
            "common_examination_questions",
            "frequently_tested_areas",
            "clinical_pearls",
            "key_points",
            "summary",
        ):
            assert expected in keys, expected
        assert len(keys) == len(set(keys)), "no duplicate section keys"

    def _payload(self, *, keys: list[str] | None = None) -> dict:
        keys = keys or [k for k, _ in ARTICLE_SECTIONS]
        return {
            "title": "A structured review",
            "sections": [
                {
                    "key": k,
                    "title": k.replace("_", " ").title(),
                    "body": "Substantive body text. " * 20,
                }
                for k in keys
            ],
            "learning_objectives": ["One.", "Two.", "Three."],
            "references": [
                {
                    "title": "Bailey & Love's Short Practice of Surgery",
                    "kind": "book",
                    "edition": "28th",
                    "publisher": "CRC Press",
                    "year": 2023,
                }
            ],
        }

    def test_a_complete_article_builds(self) -> None:
        draft = build_article(self._payload(), topic="biliary disease")
        assert len(draft.sections) == len(ARTICLE_SECTIONS)
        assert draft.word_count > 0
        assert draft.estimated_minutes >= 5

    def test_a_missing_required_section_is_a_generation_failure(self) -> None:
        keys = [k for k, _ in ARTICLE_SECTIONS if k != "management"]
        with pytest.raises(ArticleError, match="management"):
            build_article(self._payload(keys=keys), topic="biliary disease")

    def test_optional_sections_may_be_absent_for_a_non_procedural_topic(self) -> None:
        """A radiology article has no operative technique, and that is correct."""
        keys = [
            k
            for k, _ in ARTICLE_SECTIONS
            if k not in ("operative_techniques", "postoperative_care")
        ]
        draft = build_article(
            self._payload(keys=keys), topic="radiology", procedural=False
        )
        assert "operative_techniques" not in draft.section_keys
        assert not any("Operative" in w for w in draft.warnings)

    def test_a_thin_section_warns_rather_than_failing(self) -> None:
        """An article with a weak embryology section is still worth reviewing."""
        payload = self._payload()
        for section in payload["sections"]:
            if section["key"] == "embryology":
                section["body"] = "Too short."
        draft = build_article(payload, topic="biliary disease")
        assert any("Embryology" in w for w in draft.warnings)

    def test_reading_time_is_computed_not_taken_on_trust(self) -> None:
        """The model's own estimate is a guess about something we can measure."""
        payload = self._payload()
        payload["estimated_minutes"] = 999
        draft = build_article(payload, topic="biliary disease")
        assert draft.estimated_minutes != 999
        assert draft.estimated_minutes == max(5, round(draft.word_count / 200))

    def test_the_required_set_is_a_subset_of_the_prescribed_sections(self) -> None:
        assert {k for k, _ in ARTICLE_SECTIONS} >= REQUIRED_SECTIONS


class TestCitations:
    JOURNAL = Reference(
        n=1,
        authors=["Smith AB", "Jones CD"],
        title="Laparoscopic versus open appendicectomy",
        source="Br J Surg",
        year=2023,
        volume="110",
        issue="4",
        pages="412-9",
        doi="10.1093/bjs/znad001",
    )

    def test_vancouver_rendering(self) -> None:
        rendered = self.JOURNAL.render(CitationStyle.VANCOUVER)
        assert rendered.startswith("Smith AB, Jones CD.")
        assert "Br J Surg. 2023;110(4):412-9." in rendered
        assert "doi:10.1093/bjs/znad001" in rendered

    def test_apa_rendering(self) -> None:
        rendered = self.JOURNAL.render(CitationStyle.APA)
        assert "Smith AB, & Jones CD. (2023)." in rendered
        assert "Br J Surg, 110(4), 412-9." in rendered
        assert "https://doi.org/10.1093/bjs/znad001" in rendered

    def test_both_styles_come_from_one_record(self) -> None:
        """Asking an author to write each citation twice guarantees divergence."""
        payload = self.JOURNAL.as_dict()
        assert payload["vancouver"] != payload["apa"]
        assert payload["doi_url"] == "https://doi.org/10.1093/bjs/znad001"

    def test_vancouver_truncates_at_six_authors(self) -> None:
        reference = Reference(n=1, authors=[f"Author {i}" for i in range(9)], title="T")
        assert reference.render(CitationStyle.VANCOUVER).count(",") <= 7
        assert "et al" in reference.render(CitationStyle.VANCOUVER)

    def test_a_malformed_doi_is_dropped_rather_than_rendered(self) -> None:
        """A dead link that looks authoritative is worse than no link."""
        from app.services.ai.cme_author import parse_reference

        reference = parse_reference({"title": "T", "doi": "not-a-doi"}, n=1)
        assert reference.doi is None

    def test_a_doi_url_is_reduced_to_the_bare_doi(self) -> None:
        from app.services.ai.cme_author import parse_reference

        reference = parse_reference(
            {"title": "T", "doi": "https://doi.org/10.1000/abc"}, n=1
        )
        assert reference.doi == "10.1000/abc"

    def test_recognised_authorities_are_identified(self) -> None:
        book = Reference(
            n=1, title="Bailey & Love's Short Practice of Surgery", kind="book"
        )
        assert book.as_dict()["recognised_authority"]

    def test_a_bare_string_reference_is_kept_verbatim(self) -> None:
        from app.services.ai.cme_author import parse_reference

        reference = parse_reference("Some citation the model wrote as prose", n=3)
        assert reference.title == "Some citation the model wrote as prose"
        assert reference.n == 3

    def test_a_reference_list_citing_nothing_recognised_is_flagged(self) -> None:
        payload = {
            "title": "T",
            "sections": [
                {"key": k, "title": k, "body": "Body. " * 30}
                for k, _ in ARTICLE_SECTIONS
            ],
            "learning_objectives": ["A.", "B.", "C."],
            "references": [
                {"title": "An invented journal nobody has heard of", "kind": "journal"}
            ],
        }
        draft = build_article(payload, topic="biliary")
        assert any("Verify each citation exists" in w for w in draft.warnings)
