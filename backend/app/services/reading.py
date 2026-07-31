"""Reading engagement tracking and the four scores derived from it.

The specification asks for four numbers: a Reading Score, a Learning
Consistency Score, an Engagement Score and a Knowledge Retention Score. Each is
easy to state and easy to get wrong, so each one here is built around the way it
would otherwise be gamed:

* **Reading** rewards *completion*, not time. Time-based reading scores reward
  leaving a tab open, which is why ``active_seconds`` counts only heartbeats
  received while the document was visible, and why a session that never
  scrolled past the introduction scores near zero however long it lasted.
* **Consistency** rewards *spread*, not volume. A trainee who reads for six
  hours the night before an assessment and nothing else has learned less than
  one who read forty minutes on nine days, and the score has to say so.
* **Engagement** rewards *depth* — highlights, notes, references followed —
  with each signal capped, so a hundred highlights on one paragraph does not
  outrank genuine interaction across an article.
* **Retention** is the only one measured externally: performance on examination
  items covering material the trainee read at least a fortnight earlier. Recent
  reading is excluded deliberately; answering a question about something read
  an hour ago measures short-term recall, which is not what the word means.

Every score is 0-100 and stores its inputs, because a trainee will ask why.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.cbt import ExamAttempt, ExamResponse, Question
from app.models.cme import CmeAssignment, CmeResource
from app.models.enums import CmeStatus, ReadingEventKind
from app.models.learning import (
    EngagementSnapshot,
    ReadingAnnotation,
    ReadingEvent,
    ReadingSession,
)

# --------------------------------------------------------------------------
# Tunables. Institutional policy, so every one is overridable per call — the
# constants are what an institution gets before it expresses a preference.
# --------------------------------------------------------------------------
#: Scroll depth at or above which a section counts as read. Not 100: almost no
#: reader scrolls a reference list to its final line, and demanding it would
#: mark every complete read incomplete.
COMPLETION_SCROLL_PERCENT = 85.0
#: A session shorter than this is a glance, not a read, whatever it scrolled.
MINIMUM_MEANINGFUL_SECONDS = 45
#: Reading faster than this is skimming. Used to discount, never to accuse.
MAX_CREDIBLE_WORDS_PER_MINUTE = 700
#: Heartbeats further apart than this are treated as an absence, not reading.
#: The client sends one every 15 seconds; 90 tolerates a few dropped packets.
MAX_HEARTBEAT_GAP_SECONDS = 90
#: Days a topic must have been dormant before questions on it measure retention.
RETENTION_LAG_DAYS = 14

#: Engagement signal weights, and the count at which each signal saturates.
ENGAGEMENT_SIGNALS: dict[str, tuple[float, int]] = {
    # signal: (weight, count at which it is worth full marks)
    "highlights": (0.25, 8),
    "notes": (0.30, 5),
    "references_followed": (0.15, 6),
    "bookmarks": (0.10, 4),
    "videos_completed": (0.10, 2),
    "downloads": (0.10, 3),
}


class ReadingError(RuntimeError):
    """An operation is not valid for this reading session."""


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------
def open_session(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    resource: CmeResource,
    assignment_id: str | None = None,
    captured_offline: bool = False,
    now: datetime | None = None,
) -> ReadingSession:
    """Begin a reading session, marking it a revisit if the reader has been here.

    Refuses unpublished resources for the same reason the CBT engine refuses
    unpublished items: an AI-drafted article nobody has reviewed must not be
    put in front of a trainee, and certainly must not earn them credit.
    """
    now = now or utcnow()
    if not resource.is_servable:
        raise ReadingError(
            "This resource is not published and cannot be assigned or read."
        )

    prior = db.execute(
        select(func.count())
        .select_from(ReadingSession)
        .where(
            ReadingSession.user_id == user_id,
            ReadingSession.resource_id == resource.id,
        )
    ).scalar_one()

    session = ReadingSession(
        tenant_id=tenant_id,
        user_id=user_id,
        resource_id=resource.id,
        assignment_id=assignment_id,
        opened_at=now,
        is_revisit=prior > 0,
        captured_offline=captured_offline,
    )
    db.add(session)
    db.flush()
    record_event(db, session, kind=ReadingEventKind.OPENED, occurred_at=now)
    return session


def record_event(
    db: Session,
    session: ReadingSession,
    *,
    kind: str,
    occurred_at: datetime | None = None,
    delta_seconds: int = 0,
    scroll_percent: float | None = None,
    section_ref: str | None = None,
    payload: dict[str, Any] | None = None,
) -> ReadingEvent:
    """Append one telemetry event and fold it into the session roll-up.

    The roll-up is maintained here rather than recomputed on read because the
    dashboard queries it constantly and the event stream is append-only — but
    the stream stays authoritative, and :func:`recompute_session` can rebuild
    the roll-up from it whenever the two are doubted.
    """
    occurred_at = occurred_at or utcnow()
    event = ReadingEvent(
        tenant_id=session.tenant_id,
        session_id=session.id,
        kind=kind,
        occurred_at=occurred_at,
        delta_seconds=max(0, delta_seconds),
        scroll_percent=scroll_percent,
        section_ref=section_ref,
        payload=payload or {},
    )
    db.add(event)

    if kind == ReadingEventKind.HEARTBEAT:
        # Only credit a gap the client could plausibly have been present for.
        # A tab restored after two hours reports a huge delta; crediting it
        # would make the reading score a measure of browser tab hygiene.
        session.active_seconds += min(delta_seconds, MAX_HEARTBEAT_GAP_SECONDS)
    if scroll_percent is not None:
        session.max_scroll_percent = max(session.max_scroll_percent, scroll_percent)
    if (
        kind == ReadingEventKind.SECTION_COMPLETED
        and section_ref
        and section_ref not in session.sections_completed
    ):
        # Reassign rather than append: SQLAlchemy does not track in-place
        # mutation of a plain JSON column, and an appended element would be
        # silently dropped at flush.
        session.sections_completed = [*session.sections_completed, section_ref]
    if kind == ReadingEventKind.HIGHLIGHTED:
        session.highlight_count += 1
    elif kind == ReadingEventKind.NOTE_ADDED:
        session.note_count += 1
    elif kind == ReadingEventKind.BOOKMARKED:
        session.bookmark_count += 1
    elif kind == ReadingEventKind.DOWNLOADED:
        session.download_count += 1
    elif kind == ReadingEventKind.REFERENCE_FOLLOWED:
        session.reference_follow_count += 1
    elif kind == ReadingEventKind.VIDEO_STARTED:
        session.videos_started += 1
    elif kind == ReadingEventKind.VIDEO_COMPLETED:
        session.videos_completed += 1
    elif kind == ReadingEventKind.CLOSED:
        session.closed_at = occurred_at

    resource = session.resource or db.get(CmeResource, session.resource_id)
    total_sections = len(resource.sections) if resource and resource.sections else 0
    if total_sections:
        session.section_completion_percent = round(
            len(session.sections_completed) / total_sections * 100, 2
        )
    db.flush()
    return event


def recompute_session(db: Session, session: ReadingSession) -> ReadingSession:
    """Rebuild a session's roll-up from its event stream.

    The reconciliation path for offline sync, where events can arrive out of
    order or in duplicate after a device comes back online.
    """
    events = list(
        db.execute(
            select(ReadingEvent)
            .where(ReadingEvent.session_id == session.id)
            .order_by(ReadingEvent.occurred_at)
        ).scalars()
    )
    counters = {
        ReadingEventKind.HIGHLIGHTED: 0,
        ReadingEventKind.NOTE_ADDED: 0,
        ReadingEventKind.BOOKMARKED: 0,
        ReadingEventKind.DOWNLOADED: 0,
        ReadingEventKind.REFERENCE_FOLLOWED: 0,
        ReadingEventKind.VIDEO_STARTED: 0,
        ReadingEventKind.VIDEO_COMPLETED: 0,
    }
    active = 0
    max_scroll = 0.0
    sections: list[str] = []
    closed: datetime | None = None

    for event in events:
        if event.kind == ReadingEventKind.HEARTBEAT:
            active += min(event.delta_seconds, MAX_HEARTBEAT_GAP_SECONDS)
        if event.scroll_percent is not None:
            max_scroll = max(max_scroll, event.scroll_percent)
        if (
            event.kind == ReadingEventKind.SECTION_COMPLETED
            and event.section_ref
            and event.section_ref not in sections
        ):
            sections.append(event.section_ref)
        if event.kind in counters:
            counters[event.kind] += 1
        if event.kind == ReadingEventKind.CLOSED:
            closed = event.occurred_at

    session.active_seconds = active
    session.max_scroll_percent = max_scroll
    session.sections_completed = sections
    session.closed_at = closed
    session.highlight_count = counters[ReadingEventKind.HIGHLIGHTED]
    session.note_count = counters[ReadingEventKind.NOTE_ADDED]
    session.bookmark_count = counters[ReadingEventKind.BOOKMARKED]
    session.download_count = counters[ReadingEventKind.DOWNLOADED]
    session.reference_follow_count = counters[ReadingEventKind.REFERENCE_FOLLOWED]
    session.videos_started = counters[ReadingEventKind.VIDEO_STARTED]
    session.videos_completed = counters[ReadingEventKind.VIDEO_COMPLETED]

    resource = session.resource or db.get(CmeResource, session.resource_id)
    total = len(resource.sections) if resource and resource.sections else 0
    session.section_completion_percent = (
        round(len(sections) / total * 100, 2) if total else 0.0
    )
    db.flush()
    return session


def add_annotation(
    db: Session,
    session: ReadingSession,
    *,
    kind: str,
    body: str | None = None,
    quoted_text: str | None = None,
    section_ref: str | None = None,
    anchor: dict[str, Any] | None = None,
    colour: str | None = None,
) -> ReadingAnnotation:
    """Create a highlight, note or bookmark and count it against the session."""
    annotation = ReadingAnnotation(
        tenant_id=session.tenant_id,
        user_id=session.user_id,
        resource_id=session.resource_id,
        session_id=session.id,
        kind=kind,
        body=body,
        quoted_text=quoted_text,
        section_ref=section_ref,
        anchor=anchor or {},
        colour=colour,
    )
    db.add(annotation)
    event_kind = {
        "highlight": ReadingEventKind.HIGHLIGHTED,
        "note": ReadingEventKind.NOTE_ADDED,
        "bookmark": ReadingEventKind.BOOKMARKED,
    }.get(kind)
    if event_kind:
        record_event(db, session, kind=event_kind, section_ref=section_ref)
    db.flush()
    return annotation


# --------------------------------------------------------------------------
# Per-session quality
# --------------------------------------------------------------------------
def session_completion(session: ReadingSession, resource: CmeResource) -> float:
    """How much of this article this session actually covered, 0-100.

    Section completion is preferred when the article declares sections, because
    it is a claim about content rather than pixels. Scroll depth is the
    fallback. Both are gated on a plausible dwell time, which is what stops a
    scripted scroll-to-bottom from scoring 100.
    """
    if session.active_seconds < MINIMUM_MEANINGFUL_SECONDS:
        return 0.0

    if resource.sections:
        coverage = session.section_completion_percent
    else:
        coverage = min(
            100.0, session.max_scroll_percent / COMPLETION_SCROLL_PERCENT * 100
        )

    # Discount reading that outran any plausible reading speed. Not a penalty
    # and never surfaced as an accusation — it caps the credit, nothing more.
    words = resource.word_count or 0
    if words > 0 and session.active_seconds > 0:
        wpm = words * (coverage / 100) / (session.active_seconds / 60)
        if wpm > MAX_CREDIBLE_WORDS_PER_MINUTE:
            coverage *= MAX_CREDIBLE_WORDS_PER_MINUTE / wpm
    return round(min(100.0, max(0.0, coverage)), 2)


def is_complete(session: ReadingSession, resource: CmeResource) -> bool:
    return session_completion(session, resource) >= COMPLETION_SCROLL_PERCENT


# --------------------------------------------------------------------------
# The four scores
# --------------------------------------------------------------------------
@dataclass(slots=True)
class ReadingScores:
    reading: float
    consistency: float
    engagement: float
    retention: float | None
    articles_opened: int
    articles_completed: int
    active_minutes: int
    distinct_active_days: int
    components: dict[str, Any] = field(default_factory=dict)


def _reading_score(
    sessions: list[ReadingSession],
    resources: dict[str, CmeResource],
    assigned: int,
    completed_assignments: int,
) -> tuple[float, dict[str, Any]]:
    """Completion of assigned reading, with credit for reading beyond it.

    Two components. Assigned completion is the substance and carries 80%; a
    trainee who finishes everything set is doing what was asked. Self-directed
    reading carries the remaining 20% and saturates at five articles, so it can
    lift a diligent reader but never substitute for the assigned work.
    """
    assigned_share = (
        completed_assignments / assigned if assigned > 0 else None
    )
    self_directed = [s for s in sessions if s.assignment_id is None]
    completed_self = sum(
        1
        for s in self_directed
        if s.resource_id in resources and is_complete(s, resources[s.resource_id])
    )
    extra = min(1.0, completed_self / 5)

    if assigned_share is None:
        # Nothing was assigned. Scoring zero would punish a trainee for an
        # administrative omission — the same mistake the domain-score engine
        # avoids by excluding unassessed domains rather than zeroing them. Here
        # self-directed reading carries the whole score.
        score = extra * 100
        components = {
            "assigned": None,
            "assigned_completed": 0,
            "self_directed_completed": completed_self,
            "basis": "self_directed_only",
        }
    else:
        score = (assigned_share * 0.80 + extra * 0.20) * 100
        components = {
            "assigned": assigned,
            "assigned_completed": completed_assignments,
            "assigned_share": round(assigned_share, 4),
            "self_directed_completed": completed_self,
            "basis": "assigned_and_self_directed",
        }
    return round(min(100.0, score), 2), components


def _consistency_score(
    sessions: list[ReadingSession], window_start: date, window_end: date
) -> tuple[float, dict[str, Any]]:
    """How evenly reading is spread across the window.

    Normalised entropy over daily active minutes. A trainee who read the same
    amount on every day of the window scores 100; one who put it all into a
    single day scores 0. Entropy rather than a simple day count because it
    distinguishes nine light days plus one enormous one from ten even days,
    which a count cannot.
    """
    days = max(1, (window_end - window_start).days + 1)
    per_day: dict[date, int] = {}
    for session in sessions:
        day = session.opened_at.astimezone(UTC).date()
        per_day[day] = per_day.get(day, 0) + session.active_seconds

    active_days = [d for d, seconds in per_day.items() if seconds >= MINIMUM_MEANINGFUL_SECONDS]
    if not active_days:
        return 0.0, {"active_days": 0, "window_days": days, "basis": "no_activity"}
    if len(active_days) == 1:
        return 0.0, {"active_days": 1, "window_days": days, "basis": "single_day"}

    total = sum(per_day[d] for d in active_days)
    entropy = 0.0
    for day in active_days:
        share = per_day[day] / total
        entropy -= share * math.log(share)
    # Maximum entropy is log(days), attained by reading equally on every day of
    # the window — not merely on every day the trainee happened to read.
    normalised = entropy / math.log(days) if days > 1 else 0.0
    return round(min(100.0, normalised * 100), 2), {
        "active_days": len(active_days),
        "window_days": days,
        "daily_seconds": {d.isoformat(): per_day[d] for d in sorted(active_days)},
        "basis": "normalised_entropy",
    }


def _engagement_score(sessions: list[ReadingSession]) -> tuple[float, dict[str, Any]]:
    """Depth of interaction, each signal capped before weighting.

    The caps are the whole point. Without them the score is a count of
    highlights, and the fastest route to a perfect engagement score is to
    select every paragraph in one article.
    """
    if not sessions:
        return 0.0, {"basis": "no_sessions"}

    raw = {
        "highlights": sum(s.highlight_count for s in sessions),
        "notes": sum(s.note_count for s in sessions),
        "references_followed": sum(s.reference_follow_count for s in sessions),
        "bookmarks": sum(s.bookmark_count for s in sessions),
        "videos_completed": sum(s.videos_completed for s in sessions),
        "downloads": sum(s.download_count for s in sessions),
    }
    score = 0.0
    detail: dict[str, Any] = {}
    for signal, (weight, saturation) in ENGAGEMENT_SIGNALS.items():
        count = raw[signal]
        contribution = min(1.0, count / saturation) * weight
        score += contribution
        detail[signal] = {
            "count": count,
            "saturates_at": saturation,
            "weight": weight,
            "contribution": round(contribution * 100, 2),
        }
    detail["basis"] = "capped_weighted_signals"
    return round(min(100.0, score * 100), 2), detail


def _retention_score(
    db: Session,
    user_id: str,
    window_start: date,
    window_end: date,
    *,
    lag_days: int = RETENTION_LAG_DAYS,
) -> tuple[float | None, dict[str, Any]]:
    """Accuracy on items covering topics read at least ``lag_days`` earlier.

    Returns ``None``, not zero, when the trainee has not yet answered enough
    such items. A trainee in their first fortnight has no retention to measure,
    and a zero would drag their readiness score down for the offence of being
    new.
    """
    cutoff = datetime.combine(window_end, datetime.min.time(), tzinfo=UTC) - timedelta(
        days=lag_days
    )
    # ``topics`` is a JSON list column; flatten it in Python rather than
    # relying on a JSON containment operator, which differs between SQLite and
    # PostgreSQL and would make the two backends disagree about a score.
    read_topics: set[str] = set()
    topic_lists = db.execute(
        select(CmeResource.topics)
        .join(ReadingSession, ReadingSession.resource_id == CmeResource.id)
        .where(ReadingSession.user_id == user_id, ReadingSession.opened_at <= cutoff)
    ).scalars()
    for topics in topic_lists:
        for topic in topics or []:
            read_topics.add(topic)

    if not read_topics:
        return None, {"basis": "no_reading_old_enough", "lag_days": lag_days}

    window_start_dt = datetime.combine(window_start, datetime.min.time(), tzinfo=UTC)
    rows = db.execute(
        select(
            func.count(ExamResponse.id),
            func.sum(func.cast(ExamResponse.is_correct, Integer)),
        )
        .join(Question, ExamResponse.question_id == Question.id)
        .join(ExamAttempt, ExamResponse.attempt_id == ExamAttempt.id)
        .where(
            ExamAttempt.user_id == user_id,
            ExamAttempt.started_at >= window_start_dt,
            Question.topic.in_(sorted(read_topics)),
        )
    ).one()
    seen, correct = rows[0] or 0, rows[1] or 0
    if seen < 5:
        return None, {
            "basis": "insufficient_items",
            "items_seen": seen,
            "minimum": 5,
            "topics_considered": len(read_topics),
        }
    return round(correct / seen * 100, 2), {
        "basis": "accuracy_on_previously_read_topics",
        "items_seen": seen,
        "items_correct": correct,
        "topics_considered": len(read_topics),
        "lag_days": lag_days,
    }


def compute_scores(
    db: Session,
    *,
    user_id: str,
    window_start: date,
    window_end: date,
) -> ReadingScores:
    """The four reading-derived scores over one window."""
    start_dt = datetime.combine(window_start, datetime.min.time(), tzinfo=UTC)
    end_dt = datetime.combine(window_end, datetime.max.time(), tzinfo=UTC)

    sessions = list(
        db.execute(
            select(ReadingSession).where(
                ReadingSession.user_id == user_id,
                ReadingSession.opened_at >= start_dt,
                ReadingSession.opened_at <= end_dt,
            )
        ).scalars()
    )
    resource_ids = {s.resource_id for s in sessions}
    resources = {
        r.id: r
        for r in db.execute(
            select(CmeResource).where(CmeResource.id.in_(resource_ids))
        ).scalars()
    }

    assignments = list(
        db.execute(
            select(CmeAssignment).where(
                CmeAssignment.user_id == user_id,
                CmeAssignment.assigned_on <= window_end,
            )
        ).scalars()
    )
    due_in_window = [
        a for a in assignments if a.due_on is None or a.due_on >= window_start
    ]
    completed = sum(
        1
        for a in due_in_window
        if a.status in (CmeStatus.COMPLETED, CmeStatus.ASSESSED, CmeStatus.WAIVED)
    )

    reading, reading_detail = _reading_score(
        sessions, resources, len(due_in_window), completed
    )
    consistency, consistency_detail = _consistency_score(
        sessions, window_start, window_end
    )
    engagement, engagement_detail = _engagement_score(sessions)
    retention, retention_detail = _retention_score(
        db, user_id, window_start, window_end
    )

    completed_reads = sum(
        1
        for s in sessions
        if s.resource_id in resources and is_complete(s, resources[s.resource_id])
    )
    active_days = {
        s.opened_at.astimezone(UTC).date()
        for s in sessions
        if s.active_seconds >= MINIMUM_MEANINGFUL_SECONDS
    }

    return ReadingScores(
        reading=reading,
        consistency=consistency,
        engagement=engagement,
        retention=retention,
        articles_opened=len({s.resource_id for s in sessions}),
        articles_completed=completed_reads,
        active_minutes=sum(s.active_seconds for s in sessions) // 60,
        distinct_active_days=len(active_days),
        components={
            "reading": reading_detail,
            "consistency": consistency_detail,
            "engagement": engagement_detail,
            "retention": retention_detail,
        },
    )


def snapshot_scores(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    window_start: date,
    window_end: date,
    enrolment_id: str | None = None,
) -> EngagementSnapshot:
    """Compute and persist the four scores, replacing any run for this window."""
    scores = compute_scores(
        db, user_id=user_id, window_start=window_start, window_end=window_end
    )
    existing = db.execute(
        select(EngagementSnapshot).where(
            EngagementSnapshot.user_id == user_id,
            EngagementSnapshot.window_start == window_start,
            EngagementSnapshot.window_end == window_end,
        )
    ).scalar_one_or_none()

    snapshot = existing or EngagementSnapshot(
        tenant_id=tenant_id,
        user_id=user_id,
        window_start=window_start,
        window_end=window_end,
    )
    snapshot.enrolment_id = enrolment_id
    snapshot.reading_score = scores.reading
    snapshot.consistency_score = scores.consistency
    snapshot.engagement_score = scores.engagement
    snapshot.retention_score = scores.retention
    snapshot.articles_opened = scores.articles_opened
    snapshot.articles_completed = scores.articles_completed
    snapshot.active_minutes = scores.active_minutes
    snapshot.distinct_active_days = scores.distinct_active_days
    snapshot.components = scores.components
    if existing is None:
        db.add(snapshot)
    db.flush()
    return snapshot
