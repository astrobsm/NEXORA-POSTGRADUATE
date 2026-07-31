"""CBT assembly, sitting, marking and feedback.

The tests that matter most here are the negative ones. That a correct answer
marks correctly is table stakes; that an unreviewed AI-generated item can never
reach a candidate, that the timer cannot be argued with, and that feedback is
refused mid-sitting are the properties an examination depends on.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.cbt import ExamAttempt, ExamPaper, ExamResponse, Question, QuestionBank
from app.models.enums import (
    AttemptStatus,
    AuthoringSource,
    DifficultyBand,
    EditorialStatus,
    ExamMode,
    QuestionType,
)
from app.services import cbt_engine as engine


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------
def make_bank(db: Session, institution: dict) -> QuestionBank:
    bank = QuestionBank(
        tenant_id=institution["tenant"].id,
        code="BANK-1",
        name="General surgery",
    )
    db.add(bank)
    db.flush()
    return bank


def make_question(
    db: Session,
    institution: dict,
    bank: QuestionBank,
    *,
    stem: str = "A 40-year-old presents with severe epigastric pain radiating to the back.",
    correct: str = "A",
    topic: str = "pancreas",
    category: str = "clinical_medicine",
    band: str = DifficultyBand.MODERATE,
    difficulty: float = 0.4,
    status: str = EditorialStatus.PUBLISHED,
    source: str = AuthoringSource.HUMAN,
    question_type: str = QuestionType.SINGLE_BEST_ANSWER,
    marks: float = 1.0,
) -> Question:
    keys = ["A", "B", "C", "D", "E"]
    question = Question(
        tenant_id=institution["tenant"].id,
        bank_id=bank.id,
        question_type=question_type,
        stem=stem,
        lead_in="What is the single most appropriate next step?",
        options=[
            {
                "key": k,
                "text": f"Option {k} for {topic}",
                "is_correct": k == correct,
                "rationale": f"Rationale for {k}.",
            }
            for k in keys
        ],
        correct_keys=[correct],
        explanation="A sufficiently long explanation of why the key is the key.",
        references=["Bailey & Love, 28th ed."],
        topic=topic,
        blueprint_category=category,
        difficulty_band=band,
        difficulty=difficulty,
        editorial_status=status,
        authoring_source=source,
        marks=marks,
    )
    db.add(question)
    db.flush()
    return question


def make_paper(
    db: Session,
    institution: dict,
    questions: list[Question],
    *,
    duration_minutes: int = 60,
    max_attempts: int | None = None,
    published: bool = True,
    pass_mark: float = 50.0,
) -> ExamPaper:
    paper = ExamPaper(
        tenant_id=institution["tenant"].id,
        name="Weekly CBT",
        mode=ExamMode.FORMATIVE,
        question_ids=[q.id for q in questions],
        question_count=len(questions),
        duration_minutes=duration_minutes,
        pass_mark_percent=pass_mark,
        max_attempts=max_attempts,
        shuffle_questions=False,
        shuffle_options=False,
        is_published=published,
    )
    db.add(paper)
    db.flush()
    return paper


# ==========================================================================
# Quota apportionment
# ==========================================================================
class TestQuota:
    def test_proportions_sum_to_the_requested_count_exactly(self) -> None:
        """Largest-remainder apportionment, not independent rounding.

        Nine categories rounded independently routinely yields 49 or 51 items
        for a 50-item paper — a defect that only surfaces when a candidate
        counts the questions and complains.
        """
        for count in (7, 13, 50, 51, 97, 100):
            quota = engine._quota(count, engine.DEFAULT_BLUEPRINT)
            assert sum(quota.values()) == count, f"failed at {count}"

    def test_difficulty_mix_also_sums_exactly(self) -> None:
        for count in (10, 33, 50):
            quota = engine._quota(count, engine.DEFAULT_DIFFICULTY_MIX)
            assert sum(quota.values()) == count

    def test_the_specified_blueprint_is_the_default(self) -> None:
        """The curriculum fixes these proportions; they are not a suggestion."""
        assert engine.DEFAULT_BLUEPRINT["basic_sciences"] == pytest.approx(0.10)
        assert engine.DEFAULT_BLUEPRINT["clinical_medicine"] == pytest.approx(0.20)
        assert engine.DEFAULT_BLUEPRINT["operative_principles"] == pytest.approx(0.20)
        assert sum(engine.DEFAULT_BLUEPRINT.values()) == pytest.approx(1.0)

    def test_zero_count_yields_nothing(self) -> None:
        assert engine._quota(0, engine.DEFAULT_BLUEPRINT) == {}


class TestNearestBands:
    def test_substitution_prefers_an_adjacent_band(self) -> None:
        """Swapping moderate for advanced barely shifts a paper.

        Swapping it for fellowship standard changes what is being measured, so
        the ordering has to put near bands first.
        """
        order = engine._nearest_bands(DifficultyBand.MODERATE)
        assert order[0] in (DifficultyBand.EASY, DifficultyBand.ADVANCED)
        assert order[-1] == DifficultyBand.FELLOWSHIP


# ==========================================================================
# Assembly and the publication gate
# ==========================================================================
class TestAssembly:
    def test_unreviewed_ai_items_are_never_drawn(self, db: Session, institution: dict) -> None:
        """The single most important assertion in this file.

        An AI-generated item that no one has reviewed is active, complete and
        well-formed. It must still never reach a candidate.
        """
        bank = make_bank(db, institution)
        for i in range(10):
            make_question(
                db,
                institution,
                bank,
                stem=f"Unreviewed generated stem number {i} about pancreatic disease.",
                status=EditorialStatus.AI_DRAFT,
                source=AuthoringSource.AI_GENERATED,
            )
        published = make_question(
            db, institution, bank, stem="A published item about biliary colic."
        )
        db.flush()

        result = engine.assemble(
            db,
            engine.AssemblyRequest(
                tenant_id=institution["tenant"].id, bank_ids=[bank.id], count=10
            ),
        )
        assert result.question_ids == [published.id]
        assert result.pool_size == 1

    @pytest.mark.parametrize(
        "status",
        [
            EditorialStatus.DRAFT,
            EditorialStatus.IN_REVIEW,
            EditorialStatus.APPROVED,
            EditorialStatus.CHANGES_REQUESTED,
            EditorialStatus.REJECTED,
            EditorialStatus.RETIRED,
        ],
    )
    def test_only_published_is_servable(
        self, db: Session, institution: dict, status: str
    ) -> None:
        """Approved is not published. The last step is a deliberate act."""
        bank = make_bank(db, institution)
        make_question(db, institution, bank, status=status)
        db.flush()
        with pytest.raises(engine.AssemblyError):
            engine.assemble(
                db,
                engine.AssemblyRequest(
                    tenant_id=institution["tenant"].id, bank_ids=[bank.id], count=1
                ),
            )

    def test_excluded_items_are_not_served(self, db: Session, institution: dict) -> None:
        """The non-repetition guarantee."""
        bank = make_bank(db, institution)
        seen = make_question(db, institution, bank, stem="An item the candidate has met before.")
        fresh = make_question(db, institution, bank, stem="An item the candidate has not met.")
        db.flush()

        result = engine.assemble(
            db,
            engine.AssemblyRequest(
                tenant_id=institution["tenant"].id,
                bank_ids=[bank.id],
                count=2,
                exclude_question_ids=[seen.id],
            ),
        )
        assert result.question_ids == [fresh.id]

    def test_reports_shortfall_rather_than_silently_under_delivering(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        for i in range(3):
            make_question(db, institution, bank, stem=f"Only three items exist, this is {i}.")
        db.flush()

        result = engine.assemble(
            db,
            engine.AssemblyRequest(
                tenant_id=institution["tenant"].id, bank_ids=[bank.id], count=50
            ),
        )
        assert result.delivered == 3
        assert result.shortfall, "a shortfall must be reported, not hidden"

    def test_strict_mode_refuses_to_under_deliver(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        make_question(db, institution, bank)
        db.flush()
        with pytest.raises(engine.AssemblyError) as excinfo:
            engine.assemble(
                db,
                engine.AssemblyRequest(
                    tenant_id=institution["tenant"].id,
                    bank_ids=[bank.id],
                    count=50,
                    strict=True,
                ),
            )
        assert "1 of 50" in str(excinfo.value)

    def test_seeded_assembly_is_reproducible(self, db: Session, institution: dict) -> None:
        """A personalised paper must be regenerable for an appeal."""
        bank = make_bank(db, institution)
        for i in range(20):
            make_question(db, institution, bank, stem=f"Item {i} on a range of surgical topics.")
        db.flush()

        request = engine.AssemblyRequest(
            tenant_id=institution["tenant"].id, bank_ids=[bank.id], count=10, seed=1234
        )
        first = engine.assemble(db, request).question_ids
        second = engine.assemble(db, request).question_ids
        assert first == second

    def test_previously_wrong_items_are_prioritised(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        ordinary = [
            make_question(db, institution, bank, stem=f"An ordinary item numbered {i}.")
            for i in range(5)
        ]
        revisit = make_question(db, institution, bank, stem="An item they got wrong last time.")
        db.flush()

        result = engine.assemble(
            db,
            engine.AssemblyRequest(
                tenant_id=institution["tenant"].id,
                bank_ids=[bank.id],
                count=1,
                revisit_question_ids=[revisit.id],
                seed=7,
            ),
        )
        assert result.question_ids == [revisit.id]
        assert ordinary  # referenced so the intent is clear


# ==========================================================================
# Marking
# ==========================================================================
class TestMarking:
    def test_single_best_answer_is_all_or_nothing(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank, correct="C", marks=2.0)
        assert engine.mark_response(question, ["C"]) == (True, 2.0)
        assert engine.mark_response(question, ["A"]) == (False, 0.0)
        assert engine.mark_response(question, []) == (False, 0.0)

    def test_selecting_everything_scores_nothing_on_partial_credit(
        self, db: Session, institution: dict
    ) -> None:
        """Wrong selections subtract, so shotgunning cannot win.

        Without the subtraction, selecting all five options would score full
        marks on every multiple-true-false item ever written.
        """
        bank = make_bank(db, institution)
        question = make_question(
            db, institution, bank, question_type=QuestionType.MULTIPLE_TRUE_FALSE
        )
        question.correct_keys = ["A", "B"]
        db.flush()

        assert engine.mark_response(question, ["A", "B"]) == (True, 1.0)
        correct, marks = engine.mark_response(question, ["A"])
        assert not correct and marks == pytest.approx(0.5)
        assert engine.mark_response(question, ["A", "B", "C", "D", "E"])[1] == 0.0

    def test_partial_credit_never_goes_negative(
        self, db: Session, institution: dict
    ) -> None:
        """The platform does not do negative marking unless asked, and nobody has."""
        bank = make_bank(db, institution)
        question = make_question(
            db, institution, bank, question_type=QuestionType.MULTIPLE_TRUE_FALSE
        )
        question.correct_keys = ["A"]
        db.flush()
        assert engine.mark_response(question, ["B", "C", "D", "E"])[1] == 0.0

    def test_short_answer_is_never_auto_marked(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        question = make_question(
            db, institution, bank, question_type=QuestionType.SHORT_ANSWER
        )
        assert engine.mark_response(question, ["A"]) == (False, 0.0)


# ==========================================================================
# The sitting lifecycle
# ==========================================================================
class TestSitting:
    def test_start_answer_submit(self, db: Session, institution: dict) -> None:
        bank = make_bank(db, institution)
        questions = [
            make_question(db, institution, bank, stem=f"Sitting item {i} about acute abdomen.",
                          correct="A")
            for i in range(4)
        ]
        paper = make_paper(db, institution, questions)

        attempt = engine.start_attempt(
            db, paper=paper, user_id=institution["registrar"].id
        )
        assert attempt.status == AttemptStatus.IN_PROGRESS
        assert attempt.session_token, "a sitting must be issued a session token"
        assert len(attempt.served_question_ids) == 4

        for question in questions[:3]:
            engine.record_answer(
                db,
                attempt,
                question_id=question.id,
                selected_keys=["A"],
                session_token=attempt.session_token,
            )
        engine.submit_attempt(db, attempt)

        assert attempt.status == AttemptStatus.MARKED
        assert attempt.scored_marks == 3.0
        assert attempt.percent_score == pytest.approx(75.0)
        assert attempt.is_pass is True

    def test_a_second_concurrent_attempt_is_refused(
        self, db: Session, institution: dict
    ) -> None:
        """One sitting at a time. The candidate resumes rather than restarting."""
        bank = make_bank(db, institution)
        paper = make_paper(db, institution, [make_question(db, institution, bank)])
        engine.start_attempt(db, paper=paper, user_id=institution["registrar"].id)

        with pytest.raises(engine.SittingError, match="already in progress"):
            engine.start_attempt(db, paper=paper, user_id=institution["registrar"].id)

    def test_a_wrong_session_token_is_refused(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank)
        paper = make_paper(db, institution, [question])
        attempt = engine.start_attempt(
            db, paper=paper, user_id=institution["registrar"].id
        )

        with pytest.raises(engine.SittingError, match="another session"):
            engine.record_answer(
                db,
                attempt,
                question_id=question.id,
                selected_keys=["A"],
                session_token="a-token-from-somewhere-else",
            )

    def test_the_attempt_allowance_is_enforced(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        paper = make_paper(
            db, institution, [make_question(db, institution, bank)], max_attempts=1
        )
        attempt = engine.start_attempt(
            db, paper=paper, user_id=institution["registrar"].id
        )
        engine.submit_attempt(db, attempt)

        with pytest.raises(engine.SittingError, match="maximum of 1"):
            engine.start_attempt(db, paper=paper, user_id=institution["registrar"].id)

    def test_an_unpublished_paper_cannot_be_sat(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        paper = make_paper(
            db, institution, [make_question(db, institution, bank)], published=False
        )
        with pytest.raises(engine.SittingError, match="not been published"):
            engine.start_attempt(db, paper=paper, user_id=institution["registrar"].id)

    def test_a_personalised_paper_refuses_the_wrong_candidate(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        paper = make_paper(db, institution, [make_question(db, institution, bank)])
        paper.target_user_id = institution["registrar"].id
        db.flush()

        with pytest.raises(engine.SittingError, match="different candidate"):
            engine.start_attempt(db, paper=paper, user_id=institution["junior"].id)

    def test_expired_time_auto_submits_and_refuses_the_answer(
        self, db: Session, institution: dict
    ) -> None:
        """The clock is the server's.

        The client's own countdown is decoration; an answer arriving after the
        duration has elapsed is refused however recently the browser thinks it
        started.
        """
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank)
        paper = make_paper(db, institution, [question], duration_minutes=30)
        attempt = engine.start_attempt(
            db, paper=paper, user_id=institution["registrar"].id
        )
        attempt.started_at = utcnow() - timedelta(minutes=31)
        db.flush()

        with pytest.raises(engine.SittingError, match="time allowed has expired"):
            engine.record_answer(
                db,
                attempt,
                question_id=question.id,
                selected_keys=["A"],
                session_token=attempt.session_token,
            )
        assert attempt.status in (AttemptStatus.SUBMITTED, AttemptStatus.MARKED)
        assert attempt.was_auto_submitted is True

    def test_resubmitting_is_not_an_error(self, db: Session, institution: dict) -> None:
        """An auto-submit racing a candidate's Submit is ordinary, not misconduct."""
        bank = make_bank(db, institution)
        paper = make_paper(db, institution, [make_question(db, institution, bank)])
        attempt = engine.start_attempt(
            db, paper=paper, user_id=institution["registrar"].id
        )
        engine.submit_attempt(db, attempt)
        marks = attempt.scored_marks
        engine.submit_attempt(db, attempt)
        assert attempt.scored_marks == marks

    def test_cohort_percentiles_use_the_midpoint_convention(
        self, db: Session, institution: dict
    ) -> None:
        """Tied candidates share the rank rather than all taking the top of it."""
        bank = make_bank(db, institution)
        questions = [
            make_question(db, institution, bank, stem=f"Percentile item {i} on trauma.")
            for i in range(2)
        ]
        paper = make_paper(db, institution, questions)

        for user_key, answers in (
            ("registrar", ["A", "A"]),
            ("junior", ["A", "B"]),
            ("consultant", ["B", "B"]),
        ):
            attempt = engine.start_attempt(
                db, paper=paper, user_id=institution[user_key].id
            )
            for question, key in zip(questions, answers, strict=True):
                engine.record_answer(
                    db,
                    attempt,
                    question_id=question.id,
                    selected_keys=[key],
                    session_token=attempt.session_token,
                )
            engine.submit_attempt(db, attempt)

        attempts = db.query(ExamAttempt).filter_by(paper_id=paper.id).all()
        assert all(a.cohort_percentile is not None for a in attempts)
        top = max(attempts, key=lambda a: a.percent_score or 0)
        assert top.cohort_percentile == pytest.approx(83.33, abs=0.1)


# ==========================================================================
# Serving and feedback
# ==========================================================================
class TestServingAndFeedback:
    def test_served_questions_carry_no_answers(
        self, db: Session, institution: dict
    ) -> None:
        """The key must not leave the server during a sitting.

        Checked against the serialised payload rather than the object, because
        a leak would happen in serialisation.
        """
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank, correct="D")
        paper = make_paper(db, institution, [question])
        attempt = engine.start_attempt(
            db, paper=paper, user_id=institution["registrar"].id
        )

        served = engine.serve_questions(db, attempt)
        assert len(served) == 1
        for option in served[0].options:
            assert set(option) == {"key", "text"}
            assert "is_correct" not in option
            assert "rationale" not in option

    def test_option_order_is_stable_across_calls(
        self, db: Session, institution: dict
    ) -> None:
        """A candidate who reconnects must see the paper they left."""
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank)
        paper = make_paper(db, institution, [question])
        paper.shuffle_options = True
        db.flush()
        attempt = engine.start_attempt(
            db, paper=paper, user_id=institution["registrar"].id
        )

        first = [o["key"] for o in engine.serve_questions(db, attempt)[0].options]
        second = [o["key"] for o in engine.serve_questions(db, attempt)[0].options]
        assert first == second
        assert sorted(first) == ["A", "B", "C", "D", "E"]

    def test_feedback_is_refused_while_the_attempt_is_open(
        self, db: Session, institution: dict
    ) -> None:
        """Feedback mid-sitting would hand the candidate the answers."""
        bank = make_bank(db, institution)
        paper = make_paper(db, institution, [make_question(db, institution, bank)])
        attempt = engine.start_attempt(
            db, paper=paper, user_id=institution["registrar"].id
        )
        with pytest.raises(engine.SittingError, match="not available until"):
            engine.build_feedback(db, attempt)

    def test_feedback_explains_every_option(
        self, db: Session, institution: dict
    ) -> None:
        """Per-distractor rationales are what make review teach anything."""
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank, correct="B")
        paper = make_paper(db, institution, [question])
        attempt = engine.start_attempt(
            db, paper=paper, user_id=institution["registrar"].id
        )
        engine.record_answer(
            db,
            attempt,
            question_id=question.id,
            selected_keys=["C"],
            session_token=attempt.session_token,
        )
        engine.submit_attempt(db, attempt)

        feedback = engine.build_feedback(db, attempt)[0]
        assert feedback.is_correct is False
        assert feedback.correct_keys == ["B"]
        assert len(feedback.options) == 5
        assert all(o["rationale"] for o in feedback.options)
        chosen = next(o for o in feedback.options if o["key"] == "C")
        assert chosen["was_selected"] is True and chosen["is_correct"] is False
        assert feedback.references


# ==========================================================================
# Adaptive delivery
# ==========================================================================
class TestAdaptive:
    def test_target_starts_neutral(self) -> None:
        assert engine.adaptive_target(0, 0) == pytest.approx(0.5)

    def test_target_rises_with_correct_answers_and_falls_with_wrong(self) -> None:
        assert engine.adaptive_target(4, 4) > 0.5
        assert engine.adaptive_target(4, 0) < 0.5

    def test_influence_is_damped_by_evidence(self) -> None:
        """One answer must not move the target as far as sixteen do."""
        after_one = engine.adaptive_target(1, 1) - 0.5
        after_sixteen = engine.adaptive_target(16, 16) - 0.5
        assert after_sixteen > after_one

    def test_target_stays_inside_the_servable_range(self) -> None:
        """A run of correct answers must not walk off the top of the bank."""
        assert 0.05 <= engine.adaptive_target(100, 100) <= 0.95
        assert 0.05 <= engine.adaptive_target(100, 0) <= 0.95


# ==========================================================================
# Rotation helpers
# ==========================================================================
class TestRotation:
    def test_recent_items_are_reported_for_exclusion(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank)
        paper = make_paper(db, institution, [question])
        attempt = engine.start_attempt(
            db, paper=paper, user_id=institution["registrar"].id
        )
        engine.submit_attempt(db, attempt)

        recent = engine.recent_question_ids(db, institution["registrar"].id, weeks=12)
        assert question.id in recent

    def test_items_outside_the_window_rotate_back_in(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank)
        paper = make_paper(db, institution, [question])
        attempt = engine.start_attempt(
            db, paper=paper, user_id=institution["registrar"].id
        )
        engine.submit_attempt(db, attempt)
        attempt.started_at = utcnow() - timedelta(weeks=20)
        db.flush()

        assert question.id not in engine.recent_question_ids(
            db, institution["registrar"].id, weeks=12
        )

    def test_weak_topics_ignore_thin_evidence(
        self, db: Session, institution: dict
    ) -> None:
        """One wrong answer out of one is a 0% topic and means nothing.

        Without the floor the personalisation engine chases noise.
        """
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank, topic="rare_topic", correct="A")
        paper = make_paper(db, institution, [question])
        attempt = engine.start_attempt(
            db, paper=paper, user_id=institution["registrar"].id
        )
        engine.record_answer(
            db,
            attempt,
            question_id=question.id,
            selected_keys=["B"],
            session_token=attempt.session_token,
        )
        engine.submit_attempt(db, attempt)

        assert "rare_topic" not in engine.weak_topics(
            db, institution["registrar"].id, minimum_seen=4
        )
        assert "rare_topic" in engine.weak_topics(
            db, institution["registrar"].id, minimum_seen=1
        )


def test_responses_are_created_for_every_served_item(
    db: Session, institution: dict
) -> None:
    """A response row per served item, created up front.

    Serving without a row would make an unanswered item indistinguishable from
    an item that was never shown — which matters at appeal.
    """
    bank = make_bank(db, institution)
    questions = [
        make_question(db, institution, bank, stem=f"Row-per-item check number {i}.")
        for i in range(5)
    ]
    paper = make_paper(db, institution, questions)
    attempt = engine.start_attempt(db, paper=paper, user_id=institution["registrar"].id)

    rows = db.query(ExamResponse).filter_by(attempt_id=attempt.id).all()
    assert len(rows) == 5
    assert {r.sequence for r in rows} == {0, 1, 2, 3, 4}
