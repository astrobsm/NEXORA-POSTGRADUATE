"""The AI generation workflow, end to end, against the offline provider.

The point of the deterministic provider is that the whole pipeline — retrieve,
generate, validate, blueprint-check, deduplicate, balance, assemble, review,
release — is exercised in CI with no API key and no network. The provider emits
one deliberately malformed item in seven, cycling through the exact defects the
quality gate exists to catch, so a broken validator fails these tests rather
than passing them quietly.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.cbt import Question
from app.models.enums import (
    AuthoringSource,
    EditorialStatus,
    GenerationStage,
    QualityCheck,
)
from app.models.learning import GenerationJob, QuestionDraft, QuestionVersion
from app.services import editorial
from app.services.ai import pipeline, quality
from app.services.ai.provider import MockProvider, Usage, describe_provider, get_provider
from tests.test_cbt_engine import make_bank


# ==========================================================================
# The quality gate
# ==========================================================================
def sound_item(**overrides) -> dict:
    item = {
        "stem": (
            "A 52-year-old presents with a two-day history of colicky right upper "
            "quadrant pain, fever and jaundice. Observations show a temperature of "
            "38.6 degrees and a heart rate of 104."
        ),
        "lead_in": "What is the single most appropriate next step?",
        "options": [
            {"key": "A", "text": "Urgent biliary decompression", "is_correct": True,
             "rationale": "Addresses the obstructed, infected system."},
            {"key": "B", "text": "Oral antibiotics and review", "is_correct": False,
             "rationale": "Insufficient for an obstructed system."},
            {"key": "C", "text": "Outpatient ultrasound in two weeks", "is_correct": False,
             "rationale": "Delays definitive treatment unacceptably."},
            {"key": "D", "text": "Elective operating in six weeks", "is_correct": False,
             "rationale": "Does not address the acute problem."},
            {"key": "E", "text": "Discharge with analgesia", "is_correct": False,
             "rationale": "Unsafe given the systemic features."},
        ],
        "explanation": (
            "The triad described indicates an obstructed and infected biliary "
            "system, which requires decompression rather than antibiotics alone."
        ),
        "references": ["Bailey & Love, 28th ed., ch. 65."],
        "topic": "biliary",
        "blueprint_category": "clinical_medicine",
        "difficulty_band": "moderate",
    }
    item.update(overrides)
    return item


class TestQualityChecks:
    def test_a_sound_item_passes_everything(self) -> None:
        report = quality.check_item(sound_item())
        assert report.passed, report.summary

    def test_two_keys_are_rejected(self) -> None:
        item = sound_item()
        item["options"][1]["is_correct"] = True
        failures = {f.check for f in quality.check_item(item).failures}
        assert QualityCheck.EXACTLY_ONE_CORRECT in failures

    def test_four_options_are_rejected(self) -> None:
        item = sound_item()
        item["options"] = item["options"][:4]
        failures = {f.check for f in quality.check_item(item).failures}
        assert QualityCheck.OPTION_COUNT in failures

    def test_absolute_terms_are_rejected(self) -> None:
        """Candidates are taught that always/never options are wrong."""
        item = sound_item()
        item["options"][2]["text"] = "Always operate immediately"
        failures = {f.check for f in quality.check_item(item).failures}
        assert QualityCheck.NO_ABSOLUTE_TERMS in failures

    def test_all_of_the_above_is_rejected(self) -> None:
        item = sound_item()
        item["options"][4]["text"] = "All of the above"
        failures = {f.check for f in quality.check_item(item).failures}
        assert QualityCheck.NO_ALL_OF_THE_ABOVE in failures

    def test_a_missing_explanation_is_rejected(self) -> None:
        failures = {f.check for f in quality.check_item(sound_item(explanation="")).failures}
        assert QualityCheck.EXPLANATION_PRESENT in failures

    def test_missing_distractor_rationales_are_rejected(self) -> None:
        """Without them, review teaches which letter was right and nothing else."""
        item = sound_item()
        item["options"][3]["rationale"] = ""
        failures = {f.check for f in quality.check_item(item).failures}
        assert QualityCheck.DISTRACTOR_RATIONALES_PRESENT in failures

    def test_missing_references_are_rejected(self) -> None:
        failures = {f.check for f in quality.check_item(sound_item(references=[])).failures}
        assert QualityCheck.REFERENCES_PRESENT in failures

    def test_missing_curriculum_mapping_is_rejected(self) -> None:
        failures = {
            f.check for f in quality.check_item(sound_item(blueprint_category="")).failures
        }
        assert QualityCheck.CURRICULUM_MAPPED in failures

    @pytest.mark.parametrize(
        "leak",
        [
            " Hospital number MRN-4471903.",
            " The patient, Mrs A Okafor, was seen.",
            " Contact her on 08031234567.",
            " Date of birth 14/07/1972.",
            " Email chidi.eze@example.com for details.",
        ],
    )
    def test_patient_identifiers_are_rejected(self, leak: str) -> None:
        """The platform stores no identifiable data; generated items are no exception.

        A bank is exported, printed and emailed far more often than a database
        row is read, so a leak here travels further than almost anywhere else.
        """
        item = sound_item()
        item["stem"] = item["stem"] + leak
        failures = {f.check for f in quality.check_item(item).failures}
        assert QualityCheck.NO_PATIENT_IDENTIFIERS in failures, leak

    def test_a_long_correct_option_is_rejected(self) -> None:
        """The classic tell: the writer elaborates the key and clips the rest."""
        item = sound_item()
        item["options"][0]["text"] = "Urgent biliary decompression " * 12
        failures = {f.check for f in quality.check_item(item).failures}
        assert QualityCheck.HOMOGENEOUS_OPTIONS in failures

    def test_a_long_distractor_is_merely_untidy(self) -> None:
        """Only a long *correct* option gives the answer away."""
        item = sound_item()
        item["options"][3]["text"] = "Elective operating in six weeks " * 12
        failures = {f.check for f in quality.check_item(item).failures}
        assert QualityCheck.HOMOGENEOUS_OPTIONS not in failures

    def test_a_distinctive_word_shared_by_stem_and_key_is_a_cue(self) -> None:
        item = sound_item()
        item["stem"] = "A patient with confirmed choledocholithiasis and sepsis presents acutely."
        item["options"][0]["text"] = "Treat the choledocholithiasis endoscopically"
        failures = {f.check for f in quality.check_item(item).failures}
        assert QualityCheck.NO_CLUE_IN_STEM in failures

    def test_common_clinical_words_are_not_treated_as_cues(self) -> None:
        """Otherwise the check flags a large share of perfectly sound items."""
        item = sound_item()
        item["stem"] = item["stem"] + " Examination and investigations are consistent."
        item["options"][0]["text"] = "Repeat the examination after investigations"
        failures = {f.check for f in quality.check_item(item).failures}
        assert QualityCheck.NO_CLUE_IN_STEM not in failures

    def test_advisory_warnings_do_not_block(self) -> None:
        """Style notes inform a reviewer; they do not burn generation budget."""
        item = sound_item(lead_in="")
        item["stem"] = item["stem"] + " This is usually the case."
        assert quality.check_item(item).passed
        assert quality.advisory_warnings(item)


class TestDuplicateDetection:
    def test_normalisation_defeats_typographic_variation(self) -> None:
        """An em dash and a hyphen are different bytes and the same sentence.

        Without this, exact-duplicate detection is defeated by a copy-paste
        that passed through a word processor.
        """
        assert quality.normalise("Post—operative care") == quality.normalise(
            "Post-operative  care"
        )
        assert quality.normalise("Naïve résumé") == quality.normalise("Naive resume")
        assert quality.normalise("  MIXED   Case  ") == "mixed case"

    def test_normalisation_does_not_conflate_different_words(self) -> None:
        """A possessive and a plural are not the same word.

        ``patient's`` normalises to ``patient s``, not ``patients``. That is
        correct: over-aggressive normalisation would start reporting genuinely
        different items as duplicates, which is the more damaging error.
        """
        assert quality.normalise("the patient’s pain") != quality.normalise(
            "the patients pain"
        )

    def test_reordering_options_does_not_disguise_a_duplicate(self) -> None:
        """The most obvious way an item would otherwise slip past."""
        options = [{"text": "alpha"}, {"text": "beta"}, {"text": "gamma"}]
        left = quality.content_hash("A stem.", options)
        right = quality.content_hash("A stem.", list(reversed(options)))
        assert left == right

    def test_different_items_hash_differently(self) -> None:
        assert quality.content_hash("One stem.") != quality.content_hash("Another stem.")

    #: Same case, reworded: tense changed, an article inserted, a synonym
    #: swapped. These must be caught.
    PARAPHRASE_PAIRS = [
        (
            "A 52-year-old man presents with colicky right upper quadrant pain, "
            "fever and jaundice after a fatty meal.",
            "The 52-year-old man presented with colicky right upper quadrant pain, "
            "a fever and jaundice following a fatty meal.",
        ),
        (
            "A 30-year-old woman has a rapidly enlarging thyroid swelling with stridor.",
            "A 30-year-old female presents with a rapidly enlarging thyroid swelling "
            "and stridor.",
        ),
        (
            "A neonate develops bilious vomiting on day two of life.",
            "On the second day of life a neonate develops bilious vomiting.",
        ),
    ]

    #: Genuinely different items, some on the same topic. These must not be.
    DISTINCT_PAIRS = [
        (
            "A 52-year-old man presents with colicky right upper quadrant pain, "
            "fever and jaundice after a fatty meal.",
            "A 30-year-old woman is found to have asymptomatic gallstones on an "
            "ultrasound performed for an unrelated indication.",
        ),
        (
            "A 71-year-old has a painful cold lower limb of sudden onset.",
            "A 19-year-old has a rapidly enlarging neck swelling with stridor.",
        ),
        (
            "A patient with acute pancreatitis has a rising CRP on day three.",
            "A patient with a perforated duodenal ulcer has free air under the "
            "diaphragm.",
        ),
    ]

    @pytest.mark.parametrize(("left", "right"), PARAPHRASE_PAIRS)
    def test_reworded_stems_are_caught(self, left: str, right: str) -> None:
        score = quality.similarity(quality.shingles(left), quality.shingles(right))
        assert score >= quality.DUPLICATE_THRESHOLD, f"scored {score:.2f}"

    @pytest.mark.parametrize(("left", "right"), DISTINCT_PAIRS)
    def test_genuinely_different_items_are_not_duplicates(
        self, left: str, right: str
    ) -> None:
        score = quality.similarity(quality.shingles(left), quality.shingles(right))
        assert score < quality.DUPLICATE_THRESHOLD, f"scored {score:.2f}"

    def test_the_threshold_sits_between_the_two_bands_with_margin(self) -> None:
        """Pins the calibration, not just its current outcome.

        A threshold that merely happens to separate today's examples is one bad
        rewording away from being wrong. This asserts the *gap*: the worst
        paraphrase must score comfortably above the best distinct pair.
        """
        worst_paraphrase = min(
            quality.similarity(quality.shingles(a), quality.shingles(b))
            for a, b in self.PARAPHRASE_PAIRS
        )
        best_distinct = max(
            quality.similarity(quality.shingles(a), quality.shingles(b))
            for a, b in self.DISTINCT_PAIRS
        )
        assert best_distinct < quality.DUPLICATE_THRESHOLD <= worst_paraphrase
        assert worst_paraphrase - best_distinct > 0.3, "the bands must stay well apart"

    def test_a_very_short_stem_is_not_trivially_contained(self) -> None:
        """Containment would otherwise score a fragment at 1.0 against anything."""
        fragment = quality.shingles("Acute abdomen.")
        long_stem = quality.shingles(
            "A patient presents with an acute abdomen, guarding and rebound "
            "tenderness after a fall from height."
        )
        assert quality.similarity(fragment, long_stem) == 0.0

    def test_exact_matches_are_found_before_near_ones(self) -> None:
        match = quality.find_duplicate(
            item_hash="abc",
            item_shingles=["one two three"],
            known_hashes={"abc": "question-1"},
            known_shingles={},
        )
        assert match is not None
        assert match.kind == "exact" and match.existing_id == "question-1"


# ==========================================================================
# The provider seam
# ==========================================================================
class TestOfflineProvider:
    def test_it_is_the_default_when_generation_is_disabled(self) -> None:
        """Every calling path works out of the box, with no key and no spend."""
        provider = get_provider()
        assert provider.name == "mock"

    def test_it_announces_that_it_is_a_placeholder(self) -> None:
        """Nobody may mistake a mock run for real clinical content."""
        described = describe_provider(MockProvider())
        assert described["is_offline_placeholder"] is True
        warning = described["warning"].lower()
        assert "placeholder" in warning
        assert "clinically meaningless" in warning
        # It must also say how to get real content, not merely that this is not it.
        assert "rtc_enable_ai_generation" in warning

    def test_output_is_deterministic(self) -> None:
        """Which is what makes the pipeline's tests mean anything."""
        provider = MockProvider()
        schema = pipeline.question_schema(5)
        prompt = "Write 5 items.\nTopics: biliary"
        first = provider.structured(system="s", prompt=prompt, schema=schema)
        second = provider.structured(system="s", prompt=prompt, schema=schema)
        assert first.data == second.data

    def test_generated_stems_are_marked_synthetic(self) -> None:
        provider = MockProvider()
        result = provider.structured(
            system="s",
            prompt="Write 3 items.\nTopics: biliary",
            schema=pipeline.question_schema(3),
        )
        assert all("SYNTHETIC" in q["stem"] for q in result.data["questions"])

    def test_it_emits_the_defects_the_gate_exists_to_catch(self) -> None:
        """A mock that only produced perfect items would validate nothing."""
        provider = MockProvider(defect_rate=7)
        result = provider.structured(
            system="s",
            prompt="Write 21 items.\nTopics: biliary, hernia",
            schema=pipeline.question_schema(21),
        )
        items = result.data["questions"]
        failing = [q for q in items if not quality.check_item(q).passed]
        assert len(failing) == 3, "one defect in seven, cycling through the failure modes"

    def test_defects_can_be_switched_off(self) -> None:
        provider = MockProvider(defect_rate=0)
        result = provider.structured(
            system="s",
            prompt="Write 14 items.\nTopics: biliary",
            schema=pipeline.question_schema(14),
        )
        assert all(quality.check_item(q).passed for q in result.data["questions"])


class TestUsageAccounting:
    def test_cache_reads_are_billed_at_a_tenth(self) -> None:
        """Ignoring the cache would overstate a twenty-batch job substantially."""
        cached = Usage(cache_read_input_tokens=1_000_000).cost_usd(
            input_per_mtok=5.0, output_per_mtok=25.0
        )
        uncached = Usage(input_tokens=1_000_000).cost_usd(
            input_per_mtok=5.0, output_per_mtok=25.0
        )
        assert cached == pytest.approx(uncached * 0.1)

    def test_cache_writes_carry_a_premium(self) -> None:
        written = Usage(cache_creation_input_tokens=1_000_000).cost_usd(
            input_per_mtok=5.0, output_per_mtok=25.0
        )
        assert written == pytest.approx(6.25)

    def test_usage_adds(self) -> None:
        total = Usage(input_tokens=10, output_tokens=5) + Usage(
            input_tokens=1, output_tokens=2
        )
        assert (total.input_tokens, total.output_tokens) == (11, 7)


# ==========================================================================
# The pipeline
# ==========================================================================
class TestPipeline:
    def test_a_full_run_produces_reviewable_drafts(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        job = pipeline.create_job(
            db,
            tenant_id=institution["tenant"].id,
            bank_id=bank.id,
            topics=["biliary", "hernia"],
            count=10,
            requested_by_id=institution["hod"].id,
        )
        result = pipeline.run_job(db, job, provider=MockProvider())

        assert job.stage == GenerationStage.AWAITING_REVIEW
        assert result.questions, "the run must produce bank items"
        assert job.generated_count > 0
        assert job.accepted_count > 0

    def test_generated_items_land_unreviewed_and_unservable(
        self, db: Session, institution: dict
    ) -> None:
        """The requirement, checked at the point it could be violated."""
        bank = make_bank(db, institution)
        job = pipeline.create_job(
            db,
            tenant_id=institution["tenant"].id,
            bank_id=bank.id,
            topics=["biliary"],
            count=6,
        )
        result = pipeline.run_job(db, job, provider=MockProvider())

        assert result.questions
        for question in result.questions:
            assert question.editorial_status == EditorialStatus.AI_DRAFT
            assert question.authoring_source == AuthoringSource.AI_GENERATED
            assert question.is_servable is False

    def test_every_promoted_item_gets_a_version_snapshot(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        job = pipeline.create_job(
            db, tenant_id=institution["tenant"].id, bank_id=bank.id,
            topics=["biliary"], count=5,
        )
        result = pipeline.run_job(db, job, provider=MockProvider())
        for question in result.questions:
            versions = db.query(QuestionVersion).filter_by(question_id=question.id).all()
            assert len(versions) == 1
            assert versions[0].version == 1
            assert job.model in (versions[0].change_summary or "")

    def test_rejected_drafts_are_kept_with_their_reasons(
        self, db: Session, institution: dict
    ) -> None:
        """"17 items rejected" is useless; the breakdown is what fixes a prompt."""
        bank = make_bank(db, institution)
        job = pipeline.create_job(
            db, tenant_id=institution["tenant"].id, bank_id=bank.id,
            topics=["biliary"], count=12,
        )
        pipeline.run_job(db, job, provider=MockProvider(defect_rate=3))

        rejected = db.query(QuestionDraft).filter_by(job_id=job.id, is_accepted=False).all()
        assert rejected, "the mock emits defects, so some must be rejected"
        for draft in rejected:
            assert draft.rejection_reason
            assert any(not c["passed"] for c in draft.check_results)

    def test_duplicates_within_one_batch_are_caught(
        self, db: Session, institution: dict
    ) -> None:
        """Two identical items in one batch is the common case, not a rare one.

        A detector that only compared against the live bank would let a batch
        of eight identical items through in a single pass.
        """

        class RepeatingProvider(MockProvider):
            """Returns the same item over and over within each call."""

            def structured(self, **kwargs):
                response = super().structured(**kwargs)
                items = response.data["questions"]
                response.data["questions"] = [dict(items[0]) for _ in items]
                return response

        bank = make_bank(db, institution)
        job = pipeline.create_job(
            db, tenant_id=institution["tenant"].id, bank_id=bank.id,
            topics=["biliary"], count=8,
        )
        pipeline.run_job(db, job, provider=RepeatingProvider(defect_rate=0))

        assert job.duplicate_count > 0
        # At most one survivor per regeneration round: each round's prompt
        # differs, so the provider's seeded output differs, but everything
        # within a round collapses to a single distinct item.
        assert job.accepted_count <= pipeline.MAX_REGENERATION_ROUNDS
        assert job.duplicate_count > job.accepted_count

    def test_a_duplicate_of_an_existing_bank_item_is_caught(
        self, db: Session, institution: dict
    ) -> None:
        """An item already in the bank must not be generated into it again."""
        from tests.test_cbt_engine import make_question

        stem = (
            "A 47-year-old presents with obstructive jaundice, weight loss and a "
            "palpable gallbladder that is not tender to palpation."
        )

        class FixedStemProvider(MockProvider):
            """Always returns one item with a known stem."""

            def structured(self, **kwargs):
                response = super().structured(**kwargs)
                item = dict(response.data["questions"][0])
                item["stem"] = stem
                response.data["questions"] = [item]
                return response

        bank = make_bank(db, institution)
        existing = make_question(db, institution, bank, stem=stem, topic="biliary")
        existing.content_hash = quality.content_hash(stem, existing.options)
        existing.shingles = quality.shingles(stem)
        db.flush()

        job = pipeline.create_job(
            db, tenant_id=institution["tenant"].id, bank_id=bank.id,
            topics=["biliary"], count=1,
        )
        pipeline.run_job(db, job, provider=FixedStemProvider(defect_rate=0))

        assert job.duplicate_count >= 1
        assert job.accepted_count == 0
        duplicate = db.query(QuestionDraft).filter_by(job_id=job.id).first()
        assert duplicate.rejection_reason
        assert "uplicate" in duplicate.rejection_reason

    def test_each_stage_is_recorded_with_its_timing(
        self, db: Session, institution: dict
    ) -> None:
        """A stalled job must say where it stalled."""
        bank = make_bank(db, institution)
        job = pipeline.create_job(
            db, tenant_id=institution["tenant"].id, bank_id=bank.id,
            topics=["biliary"], count=5,
        )
        pipeline.run_job(db, job, provider=MockProvider())

        stages = {entry["stage"] for entry in job.stage_log}
        assert GenerationStage.RETRIEVING_KNOWLEDGE in stages
        assert GenerationStage.GENERATING in stages
        assert GenerationStage.QUALITY_VALIDATION in stages
        assert GenerationStage.BLUEPRINT_VALIDATION in stages
        assert GenerationStage.DUPLICATE_DETECTION in stages
        assert GenerationStage.DIFFICULTY_BALANCING in stages
        assert all("seconds" in entry for entry in job.stage_log)

    def test_the_service_level_is_reported_not_assumed(
        self, db: Session, institution: dict
    ) -> None:
        """A run that overshoots is reported as having overshot."""
        bank = make_bank(db, institution)
        job = pipeline.create_job(
            db, tenant_id=institution["tenant"].id, bank_id=bank.id,
            topics=["biliary"], count=5,
        )
        result = pipeline.run_job(db, job, provider=MockProvider())
        assert job.deadline_minutes == 20
        assert result.met_deadline is True
        assert job.elapsed_seconds is not None

    def test_cost_is_recorded(self, db: Session, institution: dict) -> None:
        bank = make_bank(db, institution)
        job = pipeline.create_job(
            db, tenant_id=institution["tenant"].id, bank_id=bank.id,
            topics=["biliary"], count=5,
        )
        pipeline.run_job(db, job, provider=MockProvider())
        assert job.output_tokens > 0
        assert job.estimated_cost_usd >= 0

    def test_missing_library_material_is_flagged_not_hidden(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        job = pipeline.create_job(
            db, tenant_id=institution["tenant"].id, bank_id=bank.id,
            topics=["a-topic-with-no-published-material"], count=3,
        )
        result = pipeline.run_job(db, job, provider=MockProvider())
        assert any("No published library material" in w for w in result.warnings)

    def test_a_refusal_does_not_fail_the_job(
        self, db: Session, institution: dict
    ) -> None:
        """A declined batch is a fact about that request, not a broken run."""

        class RefusingProvider(MockProvider):
            def structured(self, **kwargs):
                response = super().structured(**kwargs)
                response.refused = True
                response.refusal_category = "bio"
                response.data = None
                return response

        bank = make_bank(db, institution)
        job = pipeline.create_job(
            db, tenant_id=institution["tenant"].id, bank_id=bank.id,
            topics=["biliary"], count=3,
        )
        result = pipeline.run_job(db, job, provider=RefusingProvider())
        assert job.stage == GenerationStage.AWAITING_REVIEW
        assert result.questions == []
        assert any(entry.get("refused") for entry in job.stage_log)


class TestRelease:
    def test_release_is_refused_while_items_are_unreviewed(
        self, db: Session, institution: dict
    ) -> None:
        """The gate working, not an error to route around."""
        bank = make_bank(db, institution)
        job = pipeline.create_job(
            db, tenant_id=institution["tenant"].id, bank_id=bank.id,
            topics=["biliary"], count=5,
        )
        pipeline.run_job(db, job, provider=MockProvider(defect_rate=0))

        with pytest.raises(pipeline.PipelineError, match="approved for publication"):
            pipeline.release_paper(db, job, released_by_id=institution["hod"].id)

    def test_release_succeeds_once_every_item_is_published(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        job = pipeline.create_job(
            db, tenant_id=institution["tenant"].id, bank_id=bank.id,
            topics=["biliary"], count=4,
        )
        result = pipeline.run_job(db, job, provider=MockProvider(defect_rate=0))
        assert len(result.questions) >= 4

        for question in result.questions:
            for decision in ("submit", "approve", "publish"):
                editorial.review_question(
                    db, question, reviewer_id=institution["hod"].id, decision=decision
                )

        paper = pipeline.release_paper(
            db, job, released_by_id=institution["hod"].id, cycle_year=2026, cycle_week=31
        )
        assert paper.is_published is True
        assert len(paper.question_ids) == 4
        assert job.stage == GenerationStage.RELEASED
        assert job.released_by_id == institution["hod"].id

        served = db.query(Question).filter(Question.id.in_(paper.question_ids)).all()
        assert all(q.is_servable for q in served)


class TestKnowledgeRetrieval:
    def test_unpublished_articles_are_not_used_as_source_material(
        self, db: Session, institution: dict
    ) -> None:
        """Otherwise one unverified artefact compounds into fifty."""
        from app.models.cme import CmeResource

        db.add(
            CmeResource(
                tenant_id=institution["tenant"].id,
                title="An unreviewed AI-written article on biliary disease",
                topics=["biliary"],
                editorial_status=EditorialStatus.AI_DRAFT,
                authoring_source=AuthoringSource.AI_GENERATED,
            )
        )
        db.flush()
        sources = pipeline.retrieve_knowledge(
            db, tenant_id=institution["tenant"].id, topics=["biliary"]
        )
        assert sources == []

    def test_higher_evidence_ranks_first(self, db: Session, institution: dict) -> None:
        from app.models.cme import CmeResource

        for level, title in (("5", "An expert opinion piece"), ("1a", "A systematic review")):
            db.add(
                CmeResource(
                    tenant_id=institution["tenant"].id,
                    title=title,
                    topics=["biliary"],
                    evidence_level=level,
                    year=2024,
                    editorial_status=EditorialStatus.PUBLISHED,
                )
            )
        db.flush()
        sources = pipeline.retrieve_knowledge(
            db, tenant_id=institution["tenant"].id, topics=["biliary"]
        )
        assert sources[0]["evidence_level"] == "1a"

    def test_the_context_is_rendered_for_caching(self) -> None:
        rendered = pipeline.knowledge_context(
            [
                {
                    "title": "A guideline",
                    "evidence_level": "guideline",
                    "year": 2024,
                    "source": "NICE",
                    "doi": "10.1000/x",
                    "key_points": ["A key point."],
                    "frequently_tested_areas": ["An area."],
                }
            ]
        )
        assert "A guideline" in rendered and "10.1000/x" in rendered

    def test_no_sources_renders_nothing(self) -> None:
        assert pipeline.knowledge_context([]) == ""


def test_job_without_a_bank_or_any_bank_fails_clearly(
    db: Session, institution: dict
) -> None:
    job = pipeline.create_job(
        db, tenant_id=institution["tenant"].id, bank_id=None, topics=["biliary"], count=2
    )
    with pytest.raises(pipeline.PipelineError, match="Create a bank"):
        pipeline.run_job(db, job, provider=MockProvider(defect_rate=0))


def test_a_queued_job_records_what_was_asked_for(
    db: Session, institution: dict
) -> None:
    bank = make_bank(db, institution)
    job = pipeline.create_job(
        db,
        tenant_id=institution["tenant"].id,
        bank_id=bank.id,
        topics=["biliary", "hernia"],
        learning_objectives=["Recognise obstructive jaundice."],
        count=50,
        training_level="registrar",
    )
    assert job.stage == GenerationStage.QUEUED
    assert job.requested_count == 50
    assert job.topics == ["biliary", "hernia"]
    assert job.blueprint == pipeline.DEFAULT_BLUEPRINT
    assert sum(job.difficulty_mix.values()) == pytest.approx(1.0)
    assert isinstance(db.get(GenerationJob, job.id), GenerationJob)
