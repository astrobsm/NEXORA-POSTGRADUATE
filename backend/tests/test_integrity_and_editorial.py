"""Examination conduct and the AI publication gate.

These are the constraints the specification states as requirements rather than
features, so they are tested as requirements: not "does the happy path work"
but "can the guarantee be broken".

* Integrity monitoring is institution-configurable, so a policy with a measure
  switched off must actually store nothing.
* Camera and microphone proctoring are consent-based, so capture must be
  refused without a current consent record.
* AI behaviour flagging is advisory and never the sole basis for a penalty, so
  no code path may reach a punitive outcome without a named human.
* AI-generated content is identified until reviewed, so there must be no
  transition from generated to published that skips a person.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.cbt import Question
from app.models.enums import (
    AuthoringSource,
    EditorialStatus,
    IntegrityEventKind,
    IntegrityOutcome,
    IntegritySeverity,
    ProctoringMode,
)
from app.models.learning import IntegrityEvent, IntegrityPolicy
from app.services import cbt_engine as engine
from app.services import editorial, integrity
from tests.test_cbt_engine import make_bank, make_paper, make_question


def make_policy(db: Session, institution: dict, **overrides) -> IntegrityPolicy:
    defaults = {
        "tenant_id": institution["tenant"].id,
        "code": "STRICT",
        "name": "Summative conduct policy",
        "is_default": True,
    }
    policy = IntegrityPolicy(**{**defaults, **overrides})
    db.add(policy)
    db.flush()
    return policy


def start_sitting(db: Session, institution: dict, policy: IntegrityPolicy | None = None):
    bank = make_bank(db, institution)
    question = make_question(db, institution, bank)
    paper = make_paper(db, institution, [question])
    if policy is not None:
        paper.integrity_policy_id = policy.id
        db.flush()
    attempt = engine.start_attempt(db, paper=paper, user_id=institution["registrar"].id)
    return paper, attempt


# ==========================================================================
# Configurability
# ==========================================================================
class TestPolicyIsConfigurable:
    def test_no_policy_means_no_monitoring(self, db: Session, institution: dict) -> None:
        """The correct default for a formative quiz, and it needs no setup.

        An institution that has not configured a policy has not consented to
        surveillance, so nothing is required and nothing is logged.
        """
        paper, _ = start_sitting(db, institution)
        directives = integrity.client_directives(None, paper)
        assert directives.require_fullscreen is False
        assert directives.log_focus_changes is False
        assert directives.block_copy_paste is False
        assert directives.proctoring_mode == ProctoringMode.NONE
        assert directives.consent_required is False

    def test_focus_events_are_discarded_when_logging_is_off(
        self, db: Session, institution: dict
    ) -> None:
        """Turning a measure off must mean the data is not collected.

        Storing it anyway would breach both the institution's configuration and
        NDPR data minimisation, and would be invisible until a subject access
        request.
        """
        policy = make_policy(db, institution, log_focus_changes=False)
        _, attempt = start_sitting(db, institution, policy)

        stored = integrity.record_event(
            db, attempt, kind=IntegrityEventKind.WINDOW_BLURRED, policy=policy
        )
        assert stored is None
        assert db.query(IntegrityEvent).filter_by(attempt_id=attempt.id).count() == 0

    def test_clipboard_events_are_discarded_when_logging_is_off(
        self, db: Session, institution: dict
    ) -> None:
        policy = make_policy(db, institution, log_clipboard_attempts=False)
        _, attempt = start_sitting(db, institution, policy)
        assert (
            integrity.record_event(
                db, attempt, kind=IntegrityEventKind.PASTE_BLOCKED, policy=policy
            )
            is None
        )

    def test_events_are_stored_when_logging_is_on(
        self, db: Session, institution: dict
    ) -> None:
        policy = make_policy(db, institution, log_focus_changes=True)
        _, attempt = start_sitting(db, institution, policy)
        assert (
            integrity.record_event(
                db, attempt, kind=IntegrityEventKind.WINDOW_BLURRED, policy=policy
            )
            is not None
        )

    def test_severity_follows_the_institutions_own_thresholds(
        self, db: Session, institution: dict
    ) -> None:
        """Severity is a count against a number the institution chose."""
        policy = make_policy(
            db,
            institution,
            focus_loss_notice_threshold=2,
            focus_loss_concern_threshold=3,
        )
        _, attempt = start_sitting(db, institution, policy)

        severities = [
            integrity.record_event(
                db, attempt, kind=IntegrityEventKind.WINDOW_BLURRED, policy=policy
            ).severity
            for _ in range(4)
        ]
        assert severities[0] == IntegritySeverity.INFO
        assert severities[1] == IntegritySeverity.NOTICE
        assert severities[3] == IntegritySeverity.CONCERN

    def test_network_loss_never_escalates(self, db: Session, institution: dict) -> None:
        """Hospital wireless is unreliable; that is not suspicious behaviour."""
        policy = make_policy(db, institution)
        _, attempt = start_sitting(db, institution, policy)
        event = integrity.record_event(
            db, attempt, kind=IntegrityEventKind.NETWORK_LOST, policy=policy
        )
        assert event.severity == IntegritySeverity.INFO


# ==========================================================================
# Identifier handling
# ==========================================================================
class TestIdentifiersAreNeverStoredRaw:
    def test_fingerprints_are_hashed(self, db: Session, institution: dict) -> None:
        raw = "device-fingerprint-abc123"
        hashed = integrity.hash_identifier(raw, tenant_id=institution["tenant"].id)
        assert hashed is not None
        assert raw not in hashed
        assert len(hashed) == 32

    def test_the_same_device_hashes_differently_per_institution(self) -> None:
        """No cross-institution correlation, even if two databases are joined."""
        left = integrity.hash_identifier("same-device", tenant_id="tenant-one")
        right = integrity.hash_identifier("same-device", tenant_id="tenant-two")
        assert left != right

    def test_hashing_is_stable_within_an_institution(self) -> None:
        """It must still answer "did the device change?"."""
        first = integrity.hash_identifier("same-device", tenant_id="t")
        second = integrity.hash_identifier("same-device", tenant_id="t")
        assert first == second

    def test_absent_identifiers_stay_absent(self) -> None:
        assert integrity.hash_identifier(None, tenant_id="t") is None
        assert integrity.hash_identifier("", tenant_id="t") is None


# ==========================================================================
# Consent
# ==========================================================================
class TestConsent:
    def test_capture_is_refused_without_consent(
        self, db: Session, institution: dict
    ) -> None:
        """A policy asking for a camera is not permission to switch one on."""
        policy = make_policy(
            db, institution, proctoring_mode=ProctoringMode.CAMERA_CONSENT
        )
        _, attempt = start_sitting(db, institution, policy)
        assert integrity.may_capture_media(db, attempt, policy) == (False, False)

    def test_capture_is_permitted_once_consent_is_given(
        self, db: Session, institution: dict
    ) -> None:
        policy = make_policy(
            db, institution, proctoring_mode=ProctoringMode.CAMERA_CONSENT
        )
        _, attempt = start_sitting(db, institution, policy)
        integrity.record_consent(
            db,
            attempt=attempt,
            policy=policy,
            camera=True,
            microphone=True,
            statement_shown="You may consent to camera monitoring.",
        )
        camera, microphone = integrity.may_capture_media(db, attempt, policy)
        assert camera is True
        # The policy only asked for a camera, so consenting to a microphone
        # does not enable one. Consent cannot grant more than policy requests.
        assert microphone is False

    def test_microphone_needs_the_full_consent_mode(
        self, db: Session, institution: dict
    ) -> None:
        policy = make_policy(
            db, institution, proctoring_mode=ProctoringMode.FULL_CONSENT
        )
        _, attempt = start_sitting(db, institution, policy)
        integrity.record_consent(
            db,
            attempt=attempt,
            policy=policy,
            camera=True,
            microphone=True,
            statement_shown="Camera and microphone.",
        )
        assert integrity.may_capture_media(db, attempt, policy) == (True, True)

    def test_withdrawal_takes_effect_immediately(
        self, db: Session, institution: dict
    ) -> None:
        policy = make_policy(
            db, institution, proctoring_mode=ProctoringMode.FULL_CONSENT
        )
        _, attempt = start_sitting(db, institution, policy)
        integrity.record_consent(
            db,
            attempt=attempt,
            policy=policy,
            camera=True,
            microphone=True,
            statement_shown="Camera and microphone.",
        )
        integrity.withdraw_consent(db, attempt)
        assert integrity.may_capture_media(db, attempt, policy) == (False, False)

    def test_event_logging_mode_never_enables_media(
        self, db: Session, institution: dict
    ) -> None:
        """Even with consent on file, a logging-only policy captures nothing."""
        policy = make_policy(
            db, institution, proctoring_mode=ProctoringMode.EVENT_LOGGING
        )
        _, attempt = start_sitting(db, institution, policy)
        integrity.record_consent(
            db,
            attempt=attempt,
            policy=policy,
            camera=True,
            microphone=True,
            statement_shown="Anything.",
        )
        assert integrity.may_capture_media(db, attempt, policy) == (False, False)

    def test_the_capture_site_raises_rather_than_proceeding(
        self, db: Session, institution: dict
    ) -> None:
        policy = make_policy(
            db, institution, proctoring_mode=ProctoringMode.CAMERA_CONSENT
        )
        _, attempt = start_sitting(db, institution, policy)
        with pytest.raises(integrity.ConsentRequired):
            integrity.require_media_permission(db, attempt, policy, stream="camera")

    def test_the_exact_wording_agreed_to_is_kept(
        self, db: Session, institution: dict
    ) -> None:
        """A policy edited next term must not rewrite past consent."""
        policy = make_policy(
            db,
            institution,
            proctoring_mode=ProctoringMode.CAMERA_CONSENT,
            consent_statement="Original wording.",
        )
        _, attempt = start_sitting(db, institution, policy)
        consent = integrity.record_consent(
            db,
            attempt=attempt,
            policy=policy,
            camera=True,
            microphone=False,
            statement_shown=policy.consent_statement,
        )
        policy.consent_statement = "Completely different wording."
        db.flush()
        assert consent.statement_shown == "Original wording."


# ==========================================================================
# The report is advisory
# ==========================================================================
class TestReportIsAdvisoryOnly:
    def test_a_clean_sitting_needs_no_review(
        self, db: Session, institution: dict
    ) -> None:
        policy = make_policy(db, institution)
        _, attempt = start_sitting(db, institution, policy)
        engine.submit_attempt(db, attempt)
        report = integrity.build_report(db, attempt, policy=policy)
        assert report.outcome == IntegrityOutcome.CLEAN
        assert report.requires_human_review is False

    def test_concerns_request_a_human_rather_than_deciding(
        self, db: Session, institution: dict
    ) -> None:
        """The engine's strongest possible action is to ask for a person."""
        policy = make_policy(
            db, institution, focus_loss_concern_threshold=2, focus_loss_notice_threshold=1
        )
        _, attempt = start_sitting(db, institution, policy)
        for _ in range(5):
            integrity.record_event(
                db, attempt, kind=IntegrityEventKind.WINDOW_BLURRED, policy=policy
            )
        engine.submit_attempt(db, attempt)
        report = integrity.build_report(db, attempt, policy=policy)

        assert report.requires_human_review is True
        assert report.outcome == IntegrityOutcome.PENDING_REVIEW
        # Nothing about the result was touched.
        assert attempt.percent_score is not None
        assert attempt.is_pass is not None

    def test_every_observation_is_marked_advisory(
        self, db: Session, institution: dict
    ) -> None:
        """The flag survives being copied into an export or a hearing bundle."""
        policy = make_policy(db, institution, focus_loss_notice_threshold=1)
        _, attempt = start_sitting(db, institution, policy)
        for _ in range(3):
            integrity.record_event(
                db, attempt, kind=IntegrityEventKind.WINDOW_BLURRED, policy=policy
            )
        engine.submit_attempt(db, attempt)
        report = integrity.build_report(db, attempt, policy=policy)
        assert report.observations
        assert all(o["advisory_only"] is True for o in report.observations)

    def test_observations_describe_rather_than_conclude(
        self, db: Session, institution: dict
    ) -> None:
        """No observation may assert misconduct.

        "The window lost focus 11 times" is a fact. "The candidate was looking
        things up" is a conclusion nobody should draw from browser telemetry.
        """
        policy = make_policy(db, institution, focus_loss_notice_threshold=1)
        _, attempt = start_sitting(db, institution, policy)
        for _ in range(4):
            integrity.record_event(
                db, attempt, kind=IntegrityEventKind.WINDOW_BLURRED, policy=policy
            )
        integrity.record_event(
            db, attempt, kind=IntegrityEventKind.PASTE_BLOCKED, policy=policy
        )
        engine.submit_attempt(db, attempt)
        report = integrity.build_report(db, attempt, policy=policy)

        forbidden = ("cheat", "cheating", "misconduct", "dishonest", "guilty", "malpractice")
        for observation in report.observations:
            assert not any(w in observation["summary"].lower() for w in forbidden)

    @pytest.mark.parametrize(
        "outcome",
        ["voided", "failed", "penalised", "disqualified", IntegrityOutcome.CLEAN + "!"],
    )
    def test_a_punitive_outcome_cannot_be_recorded(
        self, db: Session, institution: dict, outcome: str
    ) -> None:
        """There is no code path from telemetry to a penalty."""
        policy = make_policy(db, institution)
        _, attempt = start_sitting(db, institution, policy)
        engine.submit_attempt(db, attempt)
        report = integrity.build_report(db, attempt, policy=policy)

        with pytest.raises(integrity.IntegrityError, match="not a decision"):
            integrity.record_review_decision(
                db,
                report,
                reviewer_id=institution["hod"].id,
                outcome=outcome,
                notes="Some notes.",
            )

    def test_a_decision_without_reasoning_is_refused(
        self, db: Session, institution: dict
    ) -> None:
        """A disposition with no reason cannot be defended at appeal."""
        policy = make_policy(db, institution)
        _, attempt = start_sitting(db, institution, policy)
        engine.submit_attempt(db, attempt)
        report = integrity.build_report(db, attempt, policy=policy)

        with pytest.raises(integrity.IntegrityError, match="reasoning"):
            integrity.record_review_decision(
                db,
                report,
                reviewer_id=institution["hod"].id,
                outcome=IntegrityOutcome.REFERRED,
                notes="   ",
            )

    def test_a_human_decision_is_recorded_with_the_human(
        self, db: Session, institution: dict
    ) -> None:
        policy = make_policy(db, institution)
        _, attempt = start_sitting(db, institution, policy)
        engine.submit_attempt(db, attempt)
        report = integrity.build_report(db, attempt, policy=policy)

        integrity.record_review_decision(
            db,
            report,
            reviewer_id=institution["hod"].id,
            outcome=IntegrityOutcome.REVIEWED_EXPLAINED,
            notes="Candidate's laptop battery failed; resumed on a ward machine.",
        )
        assert report.reviewed_by_id == institution["hod"].id
        assert report.reviewed_at is not None
        assert attempt.integrity_outcome == IntegrityOutcome.REVIEWED_EXPLAINED

    def test_recompute_does_not_reopen_a_settled_report(
        self, db: Session, institution: dict
    ) -> None:
        policy = make_policy(db, institution)
        _, attempt = start_sitting(db, institution, policy)
        engine.submit_attempt(db, attempt)
        report = integrity.build_report(db, attempt, policy=policy)
        integrity.record_review_decision(
            db,
            report,
            reviewer_id=institution["hod"].id,
            outcome=IntegrityOutcome.REVIEWED_NO_ACTION,
            notes="Nothing of concern.",
        )
        integrity.build_report(db, attempt, policy=policy)
        assert report.outcome == IntegrityOutcome.REVIEWED_NO_ACTION

    def test_a_changed_device_is_noted_but_never_blocks(
        self, db: Session, institution: dict
    ) -> None:
        """Locking a candidate out mid-examination is the worse failure."""
        policy = make_policy(db, institution)
        _, attempt = start_sitting(db, institution, policy)
        integrity.check_session_claim(
            db,
            attempt,
            session_token=attempt.session_token,
            policy=policy,
            device_fingerprint="laptop",
        )
        integrity.check_session_claim(
            db,
            attempt,
            session_token=attempt.session_token,
            policy=policy,
            device_fingerprint="ward-computer",
        )
        kinds = {
            e.kind
            for e in db.query(IntegrityEvent).filter_by(attempt_id=attempt.id).all()
        }
        assert IntegrityEventKind.DEVICE_CHANGED in kinds

    def test_a_foreign_session_token_is_refused(
        self, db: Session, institution: dict
    ) -> None:
        policy = make_policy(db, institution, single_session_only=True)
        _, attempt = start_sitting(db, institution, policy)
        with pytest.raises(integrity.IntegrityError):
            integrity.check_session_claim(
                db, attempt, session_token="not-the-issued-token", policy=policy
            )


# ==========================================================================
# The publication gate
# ==========================================================================
class TestEditorialGate:
    def test_generated_content_cannot_jump_to_published(self) -> None:
        """The single transition the requirement forbids."""
        assert not editorial.can_transition(
            EditorialStatus.AI_DRAFT, EditorialStatus.PUBLISHED
        )
        assert not editorial.can_transition(
            EditorialStatus.AI_DRAFT, EditorialStatus.APPROVED
        )

    def test_the_only_route_out_of_ai_draft_is_review(self) -> None:
        assert editorial.can_transition(
            EditorialStatus.AI_DRAFT, EditorialStatus.IN_REVIEW
        )

    def test_publishing_generated_content_needs_a_recorded_approval(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        question = make_question(
            db,
            institution,
            bank,
            status=EditorialStatus.AI_DRAFT,
            source=AuthoringSource.AI_GENERATED,
        )
        # Reach `approved` without an `approve` review having been recorded,
        # which is what a loosened transition table would allow.
        question.editorial_status = EditorialStatus.APPROVED
        db.flush()

        with pytest.raises(editorial.EditorialError, match="must be approved"):
            editorial.review_question(
                db,
                question,
                reviewer_id=institution["hod"].id,
                decision="publish",
            )

    def test_the_full_route_from_generated_to_published(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        question = make_question(
            db,
            institution,
            bank,
            status=EditorialStatus.AI_DRAFT,
            source=AuthoringSource.AI_GENERATED,
        )
        assert question.is_servable is False

        for decision in ("submit", "approve", "publish"):
            editorial.review_question(
                db, question, reviewer_id=institution["hod"].id, decision=decision
            )
        assert question.editorial_status == EditorialStatus.PUBLISHED
        assert question.is_servable is True
        assert question.published_at is not None
        assert question.reviewed_by_id == institution["hod"].id

    def test_a_rejection_must_say_why(self, db: Session, institution: dict) -> None:
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank, status=EditorialStatus.IN_REVIEW)
        with pytest.raises(editorial.EditorialError, match="must say why"):
            editorial.review_question(
                db, question, reviewer_id=institution["hod"].id, decision="reject"
            )

    def test_editing_a_published_item_returns_it_to_review(
        self, db: Session, institution: dict
    ) -> None:
        """A live item that has been altered has not been reviewed as it stands."""
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank)
        assert question.is_servable is True

        editorial.edit_question(
            db,
            question,
            editor_id=institution["hod"].id,
            changes={"explanation": "A revised and rather longer explanation entirely."},
            change_summary="Clarified the explanation.",
        )
        assert question.editorial_status == EditorialStatus.CHANGES_REQUESTED
        assert question.is_servable is False
        assert question.version == 2

    def test_human_editing_promotes_generated_to_assisted(
        self, db: Session, institution: dict
    ) -> None:
        """The honest description of jointly-authored content."""
        bank = make_bank(db, institution)
        question = make_question(
            db,
            institution,
            bank,
            status=EditorialStatus.AI_DRAFT,
            source=AuthoringSource.AI_GENERATED,
        )
        editorial.edit_question(
            db,
            question,
            editor_id=institution["hod"].id,
            changes={"topic": "hepatobiliary"},
            change_summary="Recategorised.",
        )
        assert question.authoring_source == AuthoringSource.AI_ASSISTED

    def test_editing_the_stem_refreshes_the_duplicate_fingerprint(
        self, db: Session, institution: dict
    ) -> None:
        """A stale hash would let an edited item's own duplicate through."""
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank)
        question.content_hash = "a-stale-hash"
        db.flush()

        editorial.edit_question(
            db,
            question,
            editor_id=institution["hod"].id,
            changes={"stem": "A completely different clinical vignette about trauma."},
            change_summary="Rewrote the stem.",
        )
        assert question.content_hash != "a-stale-hash"
        assert question.shingles

    def test_unknown_fields_are_refused(self, db: Session, institution: dict) -> None:
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank)
        with pytest.raises(editorial.EditorialError, match="cannot be edited"):
            editorial.edit_question(
                db,
                question,
                editor_id=institution["hod"].id,
                changes={"editorial_status": EditorialStatus.PUBLISHED},
                change_summary="Trying to publish by the back door.",
            )

    def test_every_transition_writes_an_immutable_snapshot(
        self, db: Session, institution: dict
    ) -> None:
        from app.models.learning import QuestionVersion

        bank = make_bank(db, institution)
        question = make_question(db, institution, bank, status=EditorialStatus.DRAFT)
        editorial.review_question(
            db, question, reviewer_id=institution["hod"].id, decision="submit"
        )
        versions = db.query(QuestionVersion).filter_by(question_id=question.id).all()
        assert versions
        assert versions[0].snapshot["stem"] == question.stem


class TestProvenanceDisclosure:
    def test_unreviewed_generated_content_is_labelled_loudly(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        question = make_question(
            db,
            institution,
            bank,
            status=EditorialStatus.AI_DRAFT,
            source=AuthoringSource.AI_GENERATED,
        )
        disclosure = editorial.ai_content_disclosure(question)
        assert disclosure["must_display_label"] is True
        assert disclosure["is_reviewed"] is False
        assert "NOT YET REVIEWED" in disclosure["label"]

    def test_reviewed_generated_content_says_so(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        question = make_question(
            db,
            institution,
            bank,
            status=EditorialStatus.PUBLISHED,
            source=AuthoringSource.AI_GENERATED,
        )
        disclosure = editorial.ai_content_disclosure(question)
        assert disclosure["must_display_label"] is True
        assert "reviewed and approved" in disclosure["label"]

    def test_human_content_carries_no_label(
        self, db: Session, institution: dict
    ) -> None:
        bank = make_bank(db, institution)
        question = make_question(db, institution, bank)
        disclosure = editorial.ai_content_disclosure(question)
        assert disclosure["must_display_label"] is False
        assert disclosure["label"] is None


def test_queue_summary_counts_unreviewed_generated_items(
    db: Session, institution: dict
) -> None:
    """The number a department needs to see before releasing a paper."""
    bank = make_bank(db, institution)
    for i in range(3):
        make_question(
            db,
            institution,
            bank,
            stem=f"Unreviewed generated item {i} on colorectal surgery.",
            status=EditorialStatus.AI_DRAFT,
            source=AuthoringSource.AI_GENERATED,
        )
    make_question(db, institution, bank, stem="A published human-written item.")
    db.flush()

    summary = editorial.queue_summary(db, tenant_id=institution["tenant"].id)
    assert summary["unreviewed_ai_generated"] == 3
    assert summary["published"] == 1
    assert summary["awaiting_review"] == 3


def test_review_queue_puts_least_confident_items_first(
    db: Session, institution: dict
) -> None:
    """A reviewer with an hour should spend it where the generator was unsure."""
    bank = make_bank(db, institution)
    confident = make_question(
        db, institution, bank, stem="A confident generated item.",
        status=EditorialStatus.AI_DRAFT, source=AuthoringSource.AI_GENERATED,
    )
    confident.ai_confidence = 0.95
    unsure = make_question(
        db, institution, bank, stem="An item the generator was unsure about.",
        status=EditorialStatus.AI_DRAFT, source=AuthoringSource.AI_GENERATED,
    )
    unsure.ai_confidence = 0.41
    db.flush()

    queue = editorial.review_queue(db, tenant_id=institution["tenant"].id)
    assert queue[0].question_id == unsure.id


def test_retention_purge_removes_telemetry_but_keeps_the_report(
    db: Session, institution: dict
) -> None:
    """NDPR data minimisation: the summary is the record, the stream is not."""
    from datetime import timedelta

    from app.db.base import utcnow
    from app.models.learning import IntegrityReport

    policy = make_policy(db, institution, retain_events_days=30)
    _, attempt = start_sitting(db, institution, policy)
    event = integrity.record_event(
        db, attempt, kind=IntegrityEventKind.WINDOW_BLURRED, policy=policy
    )
    engine.submit_attempt(db, attempt)
    integrity.build_report(db, attempt, policy=policy)

    event.occurred_at = utcnow() - timedelta(days=90)
    db.flush()

    removed = integrity.purge_expired_events(db, tenant_id=institution["tenant"].id)
    assert removed == 1
    assert db.query(IntegrityReport).filter_by(attempt_id=attempt.id).count() == 1
    assert db.query(IntegrityEvent).filter_by(attempt_id=attempt.id).count() == 0


def test_offline_events_are_drained_without_double_counting(
    db: Session, institution: dict
) -> None:
    """A repeated sync must not inflate the record."""
    policy = make_policy(db, institution)
    _, attempt = start_sitting(db, institution, policy)
    attempt.integrity_events = [
        {"kind": IntegrityEventKind.WINDOW_BLURRED, "duration_seconds": 4},
        {"kind": IntegrityEventKind.TAB_HIDDEN, "duration_seconds": 9},
    ]
    db.flush()

    stored = integrity.ingest_offline_events(
        db, attempt, policy=policy, events=attempt.integrity_events
    )
    assert stored == 2
    assert attempt.integrity_events == []
    assert db.query(IntegrityEvent).filter_by(attempt_id=attempt.id).count() == 2

    # Syncing again finds nothing left to drain.
    assert (
        integrity.ingest_offline_events(
            db, attempt, policy=policy, events=attempt.integrity_events
        )
        == 0
    )


def test_a_question_object_alone_cannot_authorise_serving(
    db: Session, institution: dict
) -> None:
    """`is_servable` must consider status, not just the active flag.

    Guards the property the assembler relies on, independently of the
    assembler — so a future refactor of one cannot silently break the other.
    """
    bank = make_bank(db, institution)
    question = Question(
        tenant_id=institution["tenant"].id,
        bank_id=bank.id,
        stem="An item that is active but has never been reviewed.",
        options=[{"key": "A", "text": "x", "is_correct": True}],
        correct_keys=["A"],
        is_active=True,
        editorial_status=EditorialStatus.AI_DRAFT,
        authoring_source=AuthoringSource.AI_GENERATED,
    )
    db.add(question)
    db.flush()
    assert question.is_active is True
    assert question.is_servable is False
