"""Examination conduct: policy, observation, and the post-examination report.

This module is written under three constraints the specification states
explicitly, and each is enforced here in code rather than left to convention:

1. **Integrity monitoring is institution-configurable.** Every measure is a
   column on :class:`IntegrityPolicy`. Nothing in this module assumes a measure
   is on.

2. **Camera and microphone proctoring are optional and consent-based.**
   :func:`may_capture_media` is the only path to enabling capture, and it
   requires a stored, un-withdrawn :class:`ExamConsent` row matching the policy.
   A policy that asks for camera proctoring without consent gets ``False``.

3. **AI-assisted behaviour flagging is advisory only and never the sole basis
   for a penalty.** The engine writes ``requires_human_review`` and
   ``pending_review``. It has no code path that voids an attempt, alters a
   score, or records misconduct — :func:`record_review_decision` is the only
   way past that point, and it demands a named reviewer and their reasoning.

A fourth constraint follows from NDPR and GDPR data minimisation and is honoured
throughout: raw device fingerprints and raw IP addresses are never stored. They
are salted with the institution's secret and hashed on the way in, so the
platform can answer "did the device change mid-examination?" without ever
holding an identifier it could be compelled to disclose or made to leak.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import owned_or_shared, utcnow
from app.models.cbt import ExamAttempt, ExamPaper
from app.models.enums import (
    IntegrityEventKind,
    IntegrityOutcome,
    IntegritySeverity,
    ProctoringMode,
)
from app.models.learning import (
    ExamConsent,
    IntegrityEvent,
    IntegrityPolicy,
    IntegrityReport,
)

#: Events that mean the candidate's attention left the examination window.
FOCUS_LOSS_EVENTS = {
    IntegrityEventKind.WINDOW_BLURRED,
    IntegrityEventKind.TAB_HIDDEN,
    IntegrityEventKind.FULLSCREEN_EXITED,
}

#: Events representing a blocked clipboard or print action.
CLIPBOARD_EVENTS = {
    IntegrityEventKind.COPY_BLOCKED,
    IntegrityEventKind.PASTE_BLOCKED,
    IntegrityEventKind.CUT_BLOCKED,
    IntegrityEventKind.PRINT_BLOCKED,
}

#: Events that are pure telemetry and never contribute to a review flag. A
#: candidate on a hospital wifi connection will generate dozens of these, and
#: a report that treats an unstable network as suspicious is worse than useless.
NEVER_ESCALATE = {
    IntegrityEventKind.NETWORK_LOST,
    IntegrityEventKind.NETWORK_RESTORED,
    IntegrityEventKind.WINDOW_FOCUSED,
    IntegrityEventKind.TAB_VISIBLE,
    IntegrityEventKind.FULLSCREEN_ENTERED,
    IntegrityEventKind.SESSION_RESUMED,
}


class ConsentRequired(RuntimeError):
    """Media capture was requested without a valid consent record."""


class IntegrityError(RuntimeError):
    """An integrity operation is not valid in this state."""


# --------------------------------------------------------------------------
# Policy resolution
# --------------------------------------------------------------------------
def resolve_policy(
    db: Session, paper: ExamPaper
) -> IntegrityPolicy | None:
    """The conduct policy governing this paper.

    Falls back from the paper's own policy, to the institution's default, to
    ``None``. ``None`` is a real answer meaning "no monitoring configured", and
    callers must treat it as everything switched off rather than as an error —
    an institution that has not set a policy has not consented to surveillance.
    """
    if paper.integrity_policy_id:
        policy = db.get(IntegrityPolicy, paper.integrity_policy_id)
        if policy is not None and policy.is_active:
            return policy
    return db.execute(
        select(IntegrityPolicy)
        .where(
            owned_or_shared(IntegrityPolicy.tenant_id, paper.tenant_id),
            IntegrityPolicy.is_default.is_(True),
            IntegrityPolicy.is_active.is_(True),
            IntegrityPolicy.deleted_at.is_(None),
        )
        .limit(1)
    ).scalar_one_or_none()


@dataclass(slots=True)
class ClientDirectives:
    """What the examination client is instructed to do.

    Sent to the browser at the start of a sitting. The client enforces these;
    the server records what the client reports. Neither is trusted on its own,
    which is why the timer is also enforced server-side.
    """

    require_fullscreen: bool
    block_copy_paste: bool
    block_printing: bool
    block_context_menu: bool
    log_focus_changes: bool
    log_clipboard_attempts: bool
    idle_timeout_seconds: int
    auto_submit_on_expiry: bool
    shuffle_questions: bool
    shuffle_options: bool
    proctoring_mode: str
    #: Present only when the policy asks for media capture. The candidate must
    #: be shown this and agree before any capture begins.
    consent_statement: str | None
    #: True when the candidate must actively agree before the paper opens.
    consent_required: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "require_fullscreen": self.require_fullscreen,
            "block_copy_paste": self.block_copy_paste,
            "block_printing": self.block_printing,
            "block_context_menu": self.block_context_menu,
            "log_focus_changes": self.log_focus_changes,
            "log_clipboard_attempts": self.log_clipboard_attempts,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "auto_submit_on_expiry": self.auto_submit_on_expiry,
            "shuffle_questions": self.shuffle_questions,
            "shuffle_options": self.shuffle_options,
            "proctoring_mode": self.proctoring_mode,
            "consent_statement": self.consent_statement,
            "consent_required": self.consent_required,
        }


def client_directives(
    policy: IntegrityPolicy | None, paper: ExamPaper
) -> ClientDirectives:
    """Translate a policy into instructions for the examination client.

    With no policy the answer is "monitor nothing": full screen not required,
    nothing blocked, nothing logged. That is the correct default for a
    formative practice quiz and it must not require configuration to get.
    """
    if policy is None:
        return ClientDirectives(
            require_fullscreen=False,
            block_copy_paste=False,
            block_printing=False,
            block_context_menu=False,
            log_focus_changes=False,
            log_clipboard_attempts=False,
            idle_timeout_seconds=0,
            auto_submit_on_expiry=True,
            shuffle_questions=paper.shuffle_questions,
            shuffle_options=paper.shuffle_options,
            proctoring_mode=ProctoringMode.NONE,
            consent_statement=None,
            consent_required=False,
        )

    wants_media = policy.proctoring_mode in (
        ProctoringMode.CAMERA_CONSENT,
        ProctoringMode.FULL_CONSENT,
    )
    return ClientDirectives(
        require_fullscreen=policy.require_fullscreen,
        block_copy_paste=policy.block_copy_paste,
        block_printing=policy.block_printing,
        block_context_menu=policy.block_context_menu,
        log_focus_changes=policy.log_focus_changes,
        log_clipboard_attempts=policy.log_clipboard_attempts,
        idle_timeout_seconds=policy.idle_timeout_minutes * 60,
        auto_submit_on_expiry=policy.auto_submit_on_expiry,
        shuffle_questions=policy.randomise_question_order and paper.shuffle_questions,
        shuffle_options=policy.randomise_option_order and paper.shuffle_options,
        proctoring_mode=policy.proctoring_mode,
        consent_statement=policy.consent_statement if wants_media else None,
        consent_required=wants_media and policy.require_explicit_consent,
    )


# --------------------------------------------------------------------------
# Consent
# --------------------------------------------------------------------------
def record_consent(
    db: Session,
    *,
    attempt: ExamAttempt,
    policy: IntegrityPolicy | None,
    camera: bool,
    microphone: bool,
    statement_shown: str,
    now: datetime | None = None,
) -> ExamConsent:
    """Store what the candidate was shown and what they agreed to.

    The statement is copied rather than referenced, because a policy edited
    next term must not silently rewrite what a candidate consented to today.
    """
    now = now or utcnow()
    existing = db.execute(
        select(ExamConsent).where(
            ExamConsent.attempt_id == attempt.id,
            ExamConsent.user_id == attempt.user_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.camera_granted = camera
        existing.microphone_granted = microphone
        existing.statement_shown = statement_shown
        existing.granted_at = now
        existing.withdrawn_at = None
        db.flush()
        return existing

    consent = ExamConsent(
        tenant_id=attempt.tenant_id,
        attempt_id=attempt.id,
        user_id=attempt.user_id,
        policy_id=policy.id if policy else None,
        statement_shown=statement_shown,
        camera_granted=camera,
        microphone_granted=microphone,
        granted_at=now,
    )
    db.add(consent)
    db.flush()
    return consent


def withdraw_consent(
    db: Session, attempt: ExamAttempt, *, now: datetime | None = None
) -> ExamConsent | None:
    """Withdraw consent mid-sitting. Capture must stop immediately."""
    consent = db.execute(
        select(ExamConsent).where(ExamConsent.attempt_id == attempt.id)
    ).scalar_one_or_none()
    if consent is None:
        return None
    consent.withdrawn_at = now or utcnow()
    db.flush()
    return consent


def may_capture_media(
    db: Session, attempt: ExamAttempt, policy: IntegrityPolicy | None
) -> tuple[bool, bool]:
    """Whether camera and microphone capture are permitted right now.

    Returns ``(camera, microphone)``. The only function in the codebase that
    may authorise capture. Both are ``False`` unless the policy asks for the
    stream *and* a current consent record grants it — withdrawal takes effect
    immediately, and a policy change cannot retroactively grant permission.
    """
    if policy is None or policy.proctoring_mode in (
        ProctoringMode.NONE,
        ProctoringMode.EVENT_LOGGING,
    ):
        return (False, False)

    consent = db.execute(
        select(ExamConsent).where(ExamConsent.attempt_id == attempt.id)
    ).scalar_one_or_none()
    if consent is None or not consent.is_current:
        return (False, False)

    camera = consent.camera_granted
    microphone = (
        consent.microphone_granted
        and policy.proctoring_mode == ProctoringMode.FULL_CONSENT
    )
    return (camera, microphone)


def require_media_permission(
    db: Session, attempt: ExamAttempt, policy: IntegrityPolicy | None, *, stream: str
) -> None:
    """Raise unless the named stream is permitted. For use at the capture site."""
    camera, microphone = may_capture_media(db, attempt, policy)
    allowed = camera if stream == "camera" else microphone
    if not allowed:
        raise ConsentRequired(
            f"{stream.title()} capture requires an active, consented proctoring "
            "policy for this attempt."
        )


# --------------------------------------------------------------------------
# Identifier hashing
# --------------------------------------------------------------------------
def hash_identifier(value: str | None, *, tenant_id: str) -> str | None:
    """Salted HMAC of a device fingerprint or IP address.

    Keyed on the application secret and the tenant, so the same device produces
    different hashes at different institutions and no cross-institution
    correlation is possible even if two databases are joined. Truncated to 32
    hex characters: enough to make collisions irrelevant at examination scale,
    short enough that nobody mistakes it for something reversible.
    """
    if not value:
        return None
    digest = hmac.new(
        settings.secret_key.encode(),
        f"{tenant_id}:{value}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return digest[:32]


# --------------------------------------------------------------------------
# Observation
# --------------------------------------------------------------------------
def _severity_for(
    kind: str, policy: IntegrityPolicy | None, running_focus_losses: int
) -> str:
    """Grade one event against the policy's own thresholds.

    Severity is a count against a number the institution set. Nothing here is
    a judgement about the candidate, and no severity means misconduct.
    """
    if policy is None or kind in NEVER_ESCALATE:
        return IntegritySeverity.INFO
    if kind in FOCUS_LOSS_EVENTS:
        if running_focus_losses >= policy.focus_loss_concern_threshold:
            return IntegritySeverity.CONCERN
        if running_focus_losses >= policy.focus_loss_notice_threshold:
            return IntegritySeverity.NOTICE
        return IntegritySeverity.INFO
    if kind in CLIPBOARD_EVENTS:
        return IntegritySeverity.NOTICE
    if kind in (
        IntegrityEventKind.DEVICE_CHANGED,
        IntegrityEventKind.CONCURRENT_SESSION_REFUSED,
    ):
        return IntegritySeverity.CONCERN
    if kind in (IntegrityEventKind.IP_CHANGED, IntegrityEventKind.RAPID_RESPONSE):
        return IntegritySeverity.NOTICE
    return IntegritySeverity.INFO


def record_event(
    db: Session,
    attempt: ExamAttempt,
    *,
    kind: str,
    policy: IntegrityPolicy | None = None,
    occurred_at: datetime | None = None,
    duration_seconds: int = 0,
    question_sequence: int | None = None,
    device_fingerprint: str | None = None,
    ip_address: str | None = None,
    detail: dict[str, Any] | None = None,
) -> IntegrityEvent | None:
    """Record one observation, or decline to when policy says not to.

    Returns ``None`` when the policy does not enable logging for this class of
    event. Declining is the point: an institution that has turned focus logging
    off has said it does not want that data, and quietly storing it anyway
    would be a straightforward breach of their configuration and of NDPR data
    minimisation.
    """
    if policy is not None:
        if kind in FOCUS_LOSS_EVENTS and not policy.log_focus_changes:
            return None
        if kind in CLIPBOARD_EVENTS and not policy.log_clipboard_attempts:
            return None
        if kind == IntegrityEventKind.DEVICE_CHANGED and not policy.device_fingerprinting:
            return None
        if kind == IntegrityEventKind.IP_CHANGED and not policy.ip_anomaly_detection:
            return None

    prior_focus_losses = 0
    if kind in FOCUS_LOSS_EVENTS:
        prior_focus_losses = sum(
            1
            for event in db.execute(
                select(IntegrityEvent).where(
                    IntegrityEvent.attempt_id == attempt.id,
                    IntegrityEvent.kind.in_(sorted(FOCUS_LOSS_EVENTS)),
                )
            ).scalars()
        )

    event = IntegrityEvent(
        tenant_id=attempt.tenant_id,
        attempt_id=attempt.id,
        kind=kind,
        severity=_severity_for(kind, policy, prior_focus_losses + 1),
        occurred_at=occurred_at or utcnow(),
        duration_seconds=max(0, duration_seconds),
        question_sequence=question_sequence,
        device_hash=hash_identifier(device_fingerprint, tenant_id=attempt.tenant_id),
        network_hash=hash_identifier(ip_address, tenant_id=attempt.tenant_id),
        detail=detail or {},
    )
    db.add(event)
    db.flush()
    return event


def ingest_offline_events(
    db: Session,
    attempt: ExamAttempt,
    *,
    policy: IntegrityPolicy | None,
    events: list[dict[str, Any]],
) -> int:
    """Drain the events an offline client buffered on the attempt.

    Offline sittings are a first-class case, not an exception: a trainee in a
    hospital with unreliable connectivity must be able to sit a paper and sync
    later without their integrity record being empty. Returns the number
    stored, which can be fewer than submitted when policy declines some.
    """
    stored = 0
    for raw in events:
        kind = raw.get("kind")
        if not kind:
            continue
        occurred_raw = raw.get("occurred_at")
        occurred = None
        if isinstance(occurred_raw, str):
            try:
                occurred = datetime.fromisoformat(occurred_raw)
            except ValueError:
                occurred = None
        result = record_event(
            db,
            attempt,
            kind=kind,
            policy=policy,
            occurred_at=occurred,
            duration_seconds=int(raw.get("duration_seconds") or 0),
            question_sequence=raw.get("question_sequence"),
            detail={**(raw.get("detail") or {}), "captured_offline": True},
        )
        if result is not None:
            stored += 1
    # Clear the landing area so a repeated sync does not double-count.
    attempt.integrity_events = []
    db.flush()
    return stored


def check_session_claim(
    db: Session,
    attempt: ExamAttempt,
    *,
    session_token: str,
    policy: IntegrityPolicy | None,
    device_fingerprint: str | None = None,
    ip_address: str | None = None,
) -> None:
    """Verify a request belongs to the sitting that started, and note changes.

    Raises when the token does not match and single-session enforcement is on.
    A changed device or network is recorded as an observation, never refused —
    a candidate whose laptop battery died and who resumed on a ward computer
    has done nothing wrong, and locking them out of an examination on that
    basis would be a far worse failure than the one it guards against.
    """
    single_session = policy.single_session_only if policy else True
    if attempt.session_token and session_token != attempt.session_token:
        record_event(
            db,
            attempt,
            kind=IntegrityEventKind.CONCURRENT_SESSION_REFUSED,
            policy=policy,
            device_fingerprint=device_fingerprint,
            ip_address=ip_address,
        )
        if single_session:
            raise IntegrityError("This sitting is open in another session.")

    device_hash = hash_identifier(device_fingerprint, tenant_id=attempt.tenant_id)
    if device_hash and attempt.device_hash and device_hash != attempt.device_hash:
        record_event(
            db,
            attempt,
            kind=IntegrityEventKind.DEVICE_CHANGED,
            policy=policy,
            device_fingerprint=device_fingerprint,
            detail={"previous": attempt.device_hash[:8], "current": device_hash[:8]},
        )
        attempt.device_hash = device_hash
    elif device_hash and not attempt.device_hash:
        attempt.device_hash = device_hash

    network_hash = hash_identifier(ip_address, tenant_id=attempt.tenant_id)
    if network_hash and attempt.network_hash and network_hash != attempt.network_hash:
        record_event(
            db,
            attempt,
            kind=IntegrityEventKind.IP_CHANGED,
            policy=policy,
            ip_address=ip_address,
        )
        attempt.network_hash = network_hash
    elif network_hash and not attempt.network_hash:
        attempt.network_hash = network_hash
    db.flush()


# --------------------------------------------------------------------------
# The post-examination report
# --------------------------------------------------------------------------
@dataclass(slots=True)
class Observation:
    """One plain-language finding, naming the threshold it crossed.

    Deliberately phrased as description, not accusation. "The examination
    window lost focus 11 times" is a fact; "the candidate was looking things
    up" is a conclusion nobody should draw from browser telemetry.
    """

    code: str
    severity: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "summary": self.summary,
            "detail": self.detail,
            # Restated on every observation so it survives being copied into a
            # report, an export, or a disciplinary bundle.
            "advisory_only": True,
        }


def build_report(
    db: Session,
    attempt: ExamAttempt,
    *,
    policy: IntegrityPolicy | None = None,
    now: datetime | None = None,
) -> IntegrityReport:
    """Summarise an attempt's integrity events into its one report.

    Idempotent: re-running replaces the summary in place. The report never
    changes an outcome — it sets ``requires_human_review`` and, when anything
    was flagged, leaves ``outcome`` at ``pending_review`` for a person.
    """
    now = now or utcnow()
    paper = attempt.paper or db.get(ExamPaper, attempt.paper_id)
    if policy is None and paper is not None:
        policy = resolve_policy(db, paper)

    events = list(
        db.execute(
            select(IntegrityEvent)
            .where(IntegrityEvent.attempt_id == attempt.id)
            .order_by(IntegrityEvent.occurred_at)
        ).scalars()
    )

    counts: dict[str, int] = {}
    for event in events:
        counts[event.kind] = counts.get(event.kind, 0) + 1

    focus_events = [e for e in events if e.kind in FOCUS_LOSS_EVENTS]
    focus_seconds = sum(e.duration_seconds for e in focus_events)
    clipboard = sum(counts.get(k, 0) for k in CLIPBOARD_EVENTS)
    devices = len({e.device_hash for e in events if e.device_hash})
    networks = len({e.network_hash for e in events if e.network_hash})
    rapid = counts.get(IntegrityEventKind.RAPID_RESPONSE, 0)

    observations: list[Observation] = []
    if policy is not None and focus_events:
        if len(focus_events) >= policy.focus_loss_concern_threshold:
            severity = IntegritySeverity.CONCERN
        elif len(focus_events) >= policy.focus_loss_notice_threshold:
            severity = IntegritySeverity.NOTICE
        else:
            severity = IntegritySeverity.INFO
        observations.append(
            Observation(
                code="focus_loss",
                severity=severity,
                summary=(
                    f"The examination window lost focus {len(focus_events)} time(s), "
                    f"totalling {focus_seconds} second(s). The institution's notice "
                    f"threshold is {policy.focus_loss_notice_threshold} and its "
                    f"concern threshold is {policy.focus_loss_concern_threshold}."
                ),
                detail={
                    "count": len(focus_events),
                    "total_seconds": focus_seconds,
                    "notice_threshold": policy.focus_loss_notice_threshold,
                    "concern_threshold": policy.focus_loss_concern_threshold,
                },
            )
        )

    if clipboard:
        observations.append(
            Observation(
                code="clipboard_attempts",
                severity=IntegritySeverity.NOTICE,
                summary=(
                    f"{clipboard} copy, cut, paste or print action(s) were blocked "
                    "by the examination client."
                ),
                detail={"count": clipboard},
            )
        )

    if devices > 1:
        observations.append(
            Observation(
                code="multiple_devices",
                severity=IntegritySeverity.CONCERN,
                summary=(
                    f"{devices} distinct devices were seen during this sitting. "
                    "This is expected when a candidate legitimately moves machines "
                    "and is not by itself evidence of anything."
                ),
                detail={"distinct_devices": devices},
            )
        )

    if networks > 2:
        observations.append(
            Observation(
                code="network_changes",
                severity=IntegritySeverity.INFO,
                summary=(
                    f"{networks} distinct networks were seen. Hospital wireless "
                    "commonly produces this and it is recorded for completeness."
                ),
                detail={"distinct_networks": networks},
            )
        )

    if rapid and policy is not None:
        observations.append(
            Observation(
                code="rapid_responses",
                severity=IntegritySeverity.NOTICE,
                summary=(
                    f"{rapid} answer(s) were submitted in under "
                    f"{policy.rapid_response_seconds} second(s). Fast answering is "
                    "common on items a candidate knows well."
                ),
                detail={
                    "count": rapid,
                    "threshold_seconds": policy.rapid_response_seconds,
                },
            )
        )

    if attempt.was_auto_submitted:
        observations.append(
            Observation(
                code="auto_submitted",
                severity=IntegritySeverity.INFO,
                summary="The attempt was submitted automatically when time expired.",
                detail={},
            )
        )

    # Review is warranted only by a CONCERN. Notices accumulate on the report
    # for a human who chooses to look; they do not summon one.
    requires_review = any(
        o.severity == IntegritySeverity.CONCERN for o in observations
    )

    report = db.execute(
        select(IntegrityReport).where(IntegrityReport.attempt_id == attempt.id)
    ).scalar_one_or_none()
    if report is None:
        report = IntegrityReport(
            tenant_id=attempt.tenant_id,
            attempt_id=attempt.id,
            generated_at=now,
        )
        db.add(report)

    report.policy_id = policy.id if policy else None
    report.generated_at = now
    report.event_count = len(events)
    report.event_counts = counts
    report.focus_loss_count = len(focus_events)
    report.focus_loss_seconds = focus_seconds
    report.clipboard_attempts = clipboard
    report.distinct_devices = devices
    report.distinct_networks = networks
    report.rapid_responses = rapid
    report.was_auto_submitted = attempt.was_auto_submitted
    report.observations = [o.as_dict() for o in observations]
    report.requires_human_review = requires_review

    # Only ever set the two states the engine is allowed to set. A report that
    # a human has already dispositioned is not reopened by a recompute.
    #
    # ``None`` has to be in this set. A column default is applied by SQLAlchemy
    # at INSERT, not at construction, so a report built moments ago still has
    # ``outcome is None`` here — and omitting it meant a flagged sitting was
    # never escalated on the pass that first detected it.
    if report.outcome in (None, IntegrityOutcome.CLEAN, IntegrityOutcome.PENDING_REVIEW):
        report.outcome = (
            IntegrityOutcome.PENDING_REVIEW if requires_review else IntegrityOutcome.CLEAN
        )
        attempt.integrity_outcome = report.outcome
    db.flush()
    return report


def record_review_decision(
    db: Session,
    report: IntegrityReport,
    *,
    reviewer_id: str,
    outcome: str,
    notes: str,
    candidate_statement: str | None = None,
    now: datetime | None = None,
) -> IntegrityReport:
    """A named human's disposition of a flagged report.

    The only route to any outcome beyond ``clean`` or ``pending_review``.
    Reasoning is mandatory, not because the platform is pedantic but because a
    disposition without a reason cannot be defended at appeal — and appeals are
    exactly when these records get read.
    """
    permitted = {
        IntegrityOutcome.REVIEWED_NO_ACTION,
        IntegrityOutcome.REVIEWED_EXPLAINED,
        IntegrityOutcome.REFERRED,
        IntegrityOutcome.CLEAN,
    }
    if outcome not in permitted:
        raise IntegrityError(
            f"'{outcome}' is not a decision a reviewer can record. "
            f"Permitted: {', '.join(sorted(permitted))}."
        )
    if not notes or not notes.strip():
        raise IntegrityError(
            "A review decision must record the reviewer's reasoning."
        )

    report.outcome = outcome
    report.reviewed_by_id = reviewer_id
    report.reviewed_at = now or utcnow()
    report.review_notes = notes.strip()
    if candidate_statement is not None:
        report.candidate_statement = candidate_statement

    attempt = db.get(ExamAttempt, report.attempt_id)
    if attempt is not None:
        attempt.integrity_outcome = outcome
    db.flush()
    return report


def purge_expired_events(
    db: Session, *, tenant_id: str, now: datetime | None = None
) -> int:
    """Delete integrity telemetry past its policy's retention period.

    NDPR and GDPR data minimisation: the platform keeps behavioural telemetry
    only for as long as the institution declared it needed to. The summary
    report survives — it is the examination record — but the raw event stream
    behind it does not, so a two-year-old sitting cannot be re-litigated from
    keystroke-level data nobody agreed to keep.

    Returns the number of events removed. Run from the nightly maintenance job.
    """
    now = now or utcnow()
    removed = 0
    policies = list(
        db.execute(
            select(IntegrityPolicy).where(
                owned_or_shared(IntegrityPolicy.tenant_id, tenant_id)
            )
        ).scalars()
    )
    for policy in policies:
        if policy.retain_events_days <= 0:
            continue
        cutoff = now - timedelta(days=policy.retain_events_days)
        stale = list(
            db.execute(
                select(IntegrityEvent)
                .join(
                    IntegrityReport,
                    IntegrityReport.attempt_id == IntegrityEvent.attempt_id,
                )
                .where(
                    IntegrityReport.policy_id == policy.id,
                    IntegrityEvent.occurred_at < cutoff,
                )
            ).scalars()
        )
        for event in stale:
            db.delete(event)
            removed += 1
    db.flush()
    return removed
