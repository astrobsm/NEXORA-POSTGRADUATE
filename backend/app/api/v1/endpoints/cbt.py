"""Computer-based testing: sitting, integrity, psychometrics, generation, review.

Three rules this module exists to enforce at the HTTP boundary:

* **Correct answers never leave the server during a sitting.** The serve
  endpoint returns :class:`ServedQuestion`, which has no key field; feedback is
  a different endpoint that refuses while an attempt is in progress.
* **The clock is the server's.** Every answer and every resume is checked
  against ``started_at`` plus the paper's duration. A client that says it has
  time left is not evidence.
* **Publication needs a person.** The review endpoints take the caller's
  identity from the token and record it; there is no path from generation to a
  trainee's screen that does not pass through one of them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import ClientMeta, CurrentPrincipal, DbSession, TenantId
from app.db.base import utcnow
from app.models.cbt import ExamAttempt, ExamPaper, Question
from app.models.enums import AttemptStatus, AuditAction, GenerationStage, GenerationTrigger
from app.models.learning import GenerationJob, IntegrityPolicy, IntegrityReport
from app.services import audit, editorial, integrity, psychometrics
from app.services import cbt_engine as engine
from app.services.ai import pipeline
from app.services.ai.provider import describe_provider, get_provider

router = APIRouter()


# ==========================================================================
# Request bodies
# ==========================================================================
class StartAttemptIn(BaseModel):
    enrolment_id: str | None = None
    #: An opaque browser fingerprint. Hashed with the institution's secret
    #: before storage; the raw value never reaches the database.
    device_fingerprint: str | None = None


class AnswerIn(BaseModel):
    question_id: str
    selected_keys: list[str] = Field(default_factory=list)
    seconds_spent: int = 0
    flagged_for_review: bool = False
    confidence: str | None = None
    free_text: str | None = None


class IntegrityEventIn(BaseModel):
    kind: str
    occurred_at: str | None = None
    duration_seconds: int = 0
    question_sequence: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ConsentIn(BaseModel):
    camera: bool = False
    microphone: bool = False


class IntegrityDecisionIn(BaseModel):
    outcome: str
    notes: str
    candidate_statement: str | None = None


class ReviewDecisionIn(BaseModel):
    decision: str
    comments: str | None = None
    scores: dict[str, Any] = Field(default_factory=dict)


class QuestionEditIn(BaseModel):
    changes: dict[str, Any]
    change_summary: str


class GenerationRequestIn(BaseModel):
    bank_id: str | None = None
    topics: list[str] = Field(default_factory=list)
    learning_objectives: list[str] = Field(default_factory=list)
    count: int = Field(default=50, ge=1, le=200)
    training_level: str | None = None
    org_unit_id: str | None = None
    specialty_id: str | None = None
    target_user_id: str | None = None
    trigger: str = GenerationTrigger.MANUAL
    blueprint: dict[str, float] | None = None
    difficulty_mix: dict[str, float] | None = None
    requires_review: bool = True
    #: Run immediately in this request rather than queueing. Only sensible for
    #: small batches; a fifty-item run belongs in the worker.
    run_now: bool = False


class ReleaseIn(BaseModel):
    name: str | None = None
    duration_minutes: int = 90
    pass_mark_percent: float = 50.0
    integrity_policy_id: str | None = None


# ==========================================================================
# Sitting
# ==========================================================================
def _load_attempt(db: DbSession, attempt_id: str, principal: CurrentPrincipal) -> ExamAttempt:
    attempt = db.get(ExamAttempt, attempt_id)
    if attempt is None or attempt.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attempt not found.")
    if attempt.user_id != principal.id and not principal.has("exam.result.read.any"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This attempt belongs to another candidate."
        )
    return attempt


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.post("/papers/{paper_id}/attempts", status_code=status.HTTP_201_CREATED)
def start_attempt(
    paper_id: str,
    body: StartAttemptIn,
    request: Request,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
) -> dict[str, Any]:
    """Begin a sitting and return the conduct directives the client must apply."""
    principal.require("exam.attempt.take")
    paper = db.get(ExamPaper, paper_id)
    if paper is None or paper.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found.")

    policy = integrity.resolve_policy(db, paper)
    try:
        attempt = engine.start_attempt(
            db,
            paper=paper,
            user_id=principal.id,
            enrolment_id=body.enrolment_id,
            device_hash=integrity.hash_identifier(
                body.device_fingerprint, tenant_id=tenant_id
            ),
            network_hash=integrity.hash_identifier(
                _client_ip(request), tenant_id=tenant_id
            ),
        )
    except (engine.SittingError, engine.AssemblyError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="exam_attempt", entity_id=attempt.id,
                 summary=f"Started attempt {attempt.attempt_number} at '{paper.name}'",
                 **meta)
    directives = integrity.client_directives(policy, paper)
    return {
        "attempt_id": attempt.id,
        # Presented on every subsequent request. This is what makes
        # one-session-per-candidate enforceable rather than advisory.
        "session_token": attempt.session_token,
        "attempt_number": attempt.attempt_number,
        "question_count": len(attempt.served_question_ids),
        "total_marks": attempt.total_marks,
        "remaining_seconds": engine.remaining_seconds(db, attempt),
        "directives": directives.as_dict(),
        "policy_id": policy.id if policy else None,
    }


@router.get("/attempts/{attempt_id}/questions")
def serve_questions(
    attempt_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    session_token: str = Query(..., description="Issued when the attempt started."),
) -> dict[str, Any]:
    """The paper as the candidate sees it. Contains no correct answers."""
    attempt = _load_attempt(db, attempt_id, principal)
    try:
        engine.resume_attempt(db, attempt, session_token=session_token)
    except engine.SittingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    served = engine.serve_questions(db, attempt)
    return {
        "attempt_id": attempt.id,
        "remaining_seconds": engine.remaining_seconds(db, attempt),
        "questions": [
            {
                "question_id": q.question_id,
                "sequence": q.sequence,
                "question_type": q.question_type,
                "stem": q.stem,
                "lead_in": q.lead_in,
                "options": q.options,
                "media_kind": q.media_kind,
                "media_keys": q.media_keys,
                "marks": q.marks,
                "suggested_seconds": q.suggested_seconds,
                "selected_keys": q.selected_keys,
                "flagged_for_review": q.flagged_for_review,
                "seconds_spent": q.seconds_spent,
            }
            for q in served
        ],
    }


@router.post("/attempts/{attempt_id}/answers")
def record_answer(
    attempt_id: str,
    body: AnswerIn,
    request: Request,
    db: DbSession,
    principal: CurrentPrincipal,
    session_token: str = Query(...),
) -> dict[str, Any]:
    """Store an answer. Never reveals whether it was right."""
    attempt = _load_attempt(db, attempt_id, principal)
    paper = attempt.paper or db.get(ExamPaper, attempt.paper_id)
    policy = integrity.resolve_policy(db, paper) if paper else None

    try:
        integrity.check_session_claim(
            db,
            attempt,
            session_token=session_token,
            policy=policy,
            ip_address=_client_ip(request),
        )
        engine.record_answer(
            db,
            attempt,
            question_id=body.question_id,
            selected_keys=body.selected_keys,
            seconds_spent=body.seconds_spent,
            flagged=body.flagged_for_review,
            confidence=body.confidence,
            free_text=body.free_text,
            session_token=session_token,
        )
    except (engine.SittingError, integrity.IntegrityError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    # An unusually fast answer is recorded as an observation, not a judgement.
    if policy and 0 < body.seconds_spent < policy.rapid_response_seconds:
        integrity.record_event(
            db,
            attempt,
            kind="rapid_response",
            policy=policy,
            duration_seconds=body.seconds_spent,
            detail={"threshold_seconds": policy.rapid_response_seconds},
        )
    return {"saved": True, "remaining_seconds": engine.remaining_seconds(db, attempt)}


@router.post("/attempts/{attempt_id}/submit")
def submit_attempt(
    attempt_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    meta: ClientMeta,
    session_token: str = Query(...),
) -> dict[str, Any]:
    """Mark and close the sitting, then produce its integrity report."""
    attempt = _load_attempt(db, attempt_id, principal)
    if attempt.session_token and session_token != attempt.session_token:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This sitting is open in another session."
        )

    paper = attempt.paper or db.get(ExamPaper, attempt.paper_id)
    policy = integrity.resolve_policy(db, paper) if paper else None
    if attempt.integrity_events:
        integrity.ingest_offline_events(
            db, attempt, policy=policy, events=attempt.integrity_events
        )

    engine.submit_attempt(db, attempt)
    report = integrity.build_report(db, attempt, policy=policy)
    audit.record(db, action=AuditAction.UPDATE, tenant_id=attempt.tenant_id,
                 actor_id=principal.id, entity_type="exam_attempt", entity_id=attempt.id,
                 summary=f"Submitted attempt, {attempt.percent_score}%", **meta)
    return {
        "attempt_id": attempt.id,
        "status": attempt.status,
        "scored_marks": attempt.scored_marks,
        "total_marks": attempt.total_marks,
        "percent_score": attempt.percent_score,
        "is_pass": attempt.is_pass,
        "topic_breakdown": attempt.topic_breakdown,
        "cohort_percentile": attempt.cohort_percentile,
        "integrity": {
            "outcome": report.outcome,
            # Advisory only. Never a penalty, never a verdict on the candidate.
            "requires_human_review": report.requires_human_review,
            "observations": report.observations,
        },
    }


@router.get("/attempts/{attempt_id}/feedback")
def attempt_feedback(
    attempt_id: str, db: DbSession, principal: CurrentPrincipal
) -> dict[str, Any]:
    """Per-question review: why the key is right and each distractor wrong."""
    attempt = _load_attempt(db, attempt_id, principal)
    try:
        feedback = engine.build_feedback(db, attempt)
    except engine.SittingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return {
        "attempt_id": attempt.id,
        "percent_score": attempt.percent_score,
        "is_pass": attempt.is_pass,
        "cohort_percentile": attempt.cohort_percentile,
        "questions": [
            {
                "question_id": f.question_id,
                "sequence": f.sequence,
                "stem": f.stem,
                "lead_in": f.lead_in,
                "selected_keys": f.selected_keys,
                "correct_keys": f.correct_keys,
                "is_correct": f.is_correct,
                "marks_awarded": f.marks_awarded,
                "marks_available": f.marks_available,
                "seconds_spent": f.seconds_spent,
                "options": f.options,
                "explanation": f.explanation,
                "references": f.references,
                "topic": f.topic,
                "difficulty_band": f.difficulty_band,
                "cohort_facility": f.cohort_facility,
                "cme_resource_id": f.cme_resource_id,
                "authoring_source": f.authoring_source,
            }
            for f in feedback
        ],
    }


# ==========================================================================
# Integrity
# ==========================================================================
@router.post("/attempts/{attempt_id}/integrity-events", status_code=status.HTTP_202_ACCEPTED)
def post_integrity_events(
    attempt_id: str,
    events: list[IntegrityEventIn],
    db: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    """Accept observations from the examination client.

    Returns how many were stored, which can be fewer than sent: the policy
    decides what is logged, and an institution that turned focus logging off
    gets its events discarded rather than quietly kept.
    """
    attempt = _load_attempt(db, attempt_id, principal)
    paper = attempt.paper or db.get(ExamPaper, attempt.paper_id)
    policy = integrity.resolve_policy(db, paper) if paper else None
    stored = integrity.ingest_offline_events(
        db, attempt, policy=policy, events=[e.model_dump() for e in events]
    )
    return {"received": len(events), "stored": stored}


@router.post("/attempts/{attempt_id}/consent")
def give_consent(
    attempt_id: str, body: ConsentIn, db: DbSession, principal: CurrentPrincipal
) -> dict[str, Any]:
    """Record consent for optional camera or microphone proctoring."""
    attempt = _load_attempt(db, attempt_id, principal)
    if attempt.user_id != principal.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the candidate can give their own consent."
        )
    paper = attempt.paper or db.get(ExamPaper, attempt.paper_id)
    policy = integrity.resolve_policy(db, paper) if paper else None
    statement = (policy.consent_statement if policy else None) or (
        "No proctoring statement is configured for this examination."
    )
    integrity.record_consent(
        db,
        attempt=attempt,
        policy=policy,
        camera=body.camera,
        microphone=body.microphone,
        statement_shown=statement,
    )
    camera, microphone = integrity.may_capture_media(db, attempt, policy)
    return {"camera_enabled": camera, "microphone_enabled": microphone}


@router.delete("/attempts/{attempt_id}/consent")
def withdraw_consent(
    attempt_id: str, db: DbSession, principal: CurrentPrincipal
) -> dict[str, Any]:
    """Withdraw consent. Capture stops immediately."""
    attempt = _load_attempt(db, attempt_id, principal)
    if attempt.user_id != principal.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the candidate can withdraw their consent."
        )
    integrity.withdraw_consent(db, attempt)
    return {"camera_enabled": False, "microphone_enabled": False}


@router.get("/attempts/{attempt_id}/integrity-report")
def get_integrity_report(
    attempt_id: str, db: DbSession, principal: CurrentPrincipal
) -> dict[str, Any]:
    principal.require("exam.integrity.review")
    attempt = _load_attempt(db, attempt_id, principal)
    report = db.execute(
        select(IntegrityReport).where(IntegrityReport.attempt_id == attempt.id)
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No integrity report yet.")
    return {
        **report.as_dict(),
        "advisory_notice": (
            "These observations are advisory. They describe what the examination "
            "client reported and are never, on their own, a basis for any finding "
            "against a candidate."
        ),
    }


@router.post("/attempts/{attempt_id}/integrity-report/decision")
def decide_integrity(
    attempt_id: str,
    body: IntegrityDecisionIn,
    db: DbSession,
    principal: CurrentPrincipal,
    meta: ClientMeta,
) -> dict[str, Any]:
    """A named reviewer's disposition. The only route past ``pending_review``."""
    principal.require("exam.integrity.review")
    attempt = _load_attempt(db, attempt_id, principal)
    report = db.execute(
        select(IntegrityReport).where(IntegrityReport.attempt_id == attempt.id)
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No integrity report yet.")
    try:
        integrity.record_review_decision(
            db,
            report,
            reviewer_id=principal.id,
            outcome=body.outcome,
            notes=body.notes,
            candidate_statement=body.candidate_statement,
        )
    except integrity.IntegrityError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    audit.record(db, action=AuditAction.UPDATE, tenant_id=report.tenant_id,
                 actor_id=principal.id, entity_type="integrity_report",
                 entity_id=report.id,
                 summary=f"Integrity report dispositioned as '{body.outcome}'", **meta)
    return {"outcome": report.outcome, "reviewed_by": principal.id}


@router.get("/integrity-policies")
def list_policies(
    db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId
) -> list[dict[str, Any]]:
    principal.require("exam.integrity.configure")
    rows = db.execute(
        select(IntegrityPolicy).where(
            IntegrityPolicy.tenant_id == tenant_id,
            IntegrityPolicy.deleted_at.is_(None),
        )
    ).scalars()
    return [row.as_dict() for row in rows]


# ==========================================================================
# Psychometrics
# ==========================================================================
@router.get("/papers/{paper_id}/analysis")
def paper_analysis(
    paper_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    persist: bool = Query(False, description="Store the analysis as well as return it."),
) -> dict[str, Any]:
    """Item and paper statistics across every marked attempt."""
    principal.require("exam.psychometrics.read")
    paper = db.get(ExamPaper, paper_id)
    if paper is None or paper.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found.")

    stats = psychometrics.analyse_paper(db, paper, persist=persist)
    if stats is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "At least two candidates must have submitted before any statistic is "
            "defined for this paper.",
        )
    return {
        "paper_id": stats.paper_id,
        "candidates": stats.candidates,
        "items": stats.items,
        "mean_percent": stats.mean_percent,
        "sd_percent": stats.sd_percent,
        "median_percent": stats.median_percent,
        "pass_rate": stats.pass_rate,
        "kr20": stats.kr20,
        "cronbach_alpha": stats.cronbach_alpha,
        "sem": stats.sem,
        "mean_facility": stats.mean_facility,
        "mean_discrimination": stats.mean_discrimination,
        "is_defensible": stats.is_defensible,
        "minimum_defensible_reliability": psychometrics.MINIMUM_DEFENSIBLE_RELIABILITY,
        "blueprint_coverage": stats.blueprint_coverage,
        "flagged_items": stats.flagged_items,
        "items_detail": [
            {
                "question_id": i.question_id,
                "facility": i.facility,
                "difficulty": i.difficulty,
                "band": i.band,
                "discrimination": i.discrimination,
                "discrimination_index": i.discrimination_index,
                "mean_seconds": i.mean_seconds,
                "distractor_stats": i.distractor_stats,
                "flags": i.flags,
            }
            for i in stats.item_statistics
        ],
    }


# ==========================================================================
# Editorial review
# ==========================================================================
@router.get("/review-queue")
def get_review_queue(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    bank_id: str | None = None,
    generation_job_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """Items awaiting a human, lowest generator confidence first."""
    principal.require("exam.question.review")
    entries = editorial.review_queue(
        db,
        tenant_id=tenant_id,
        bank_id=bank_id,
        generation_job_id=generation_job_id,
        limit=limit,
    )
    return {
        "summary": editorial.queue_summary(db, tenant_id=tenant_id),
        "items": [e.as_dict() for e in entries],
    }


@router.get("/questions/{question_id}")
def get_question_for_review(
    question_id: str, db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId
) -> dict[str, Any]:
    """The full item including its key. Requires the review permission."""
    principal.require("exam.question.review")
    question = db.get(Question, question_id)
    if question is None or question.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Question not found.")
    return {
        **question.as_dict(),
        "provenance": editorial.ai_content_disclosure(question),
        "is_servable": question.is_servable,
    }


@router.post("/questions/{question_id}/review")
def review_question(
    question_id: str,
    body: ReviewDecisionIn,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
) -> dict[str, Any]:
    """Approve, reject, request changes on, publish or retire an item."""
    principal.require("exam.question.review")
    question = db.get(Question, question_id)
    if question is None or question.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Question not found.")
    try:
        review = editorial.review_question(
            db,
            question,
            reviewer_id=principal.id,
            decision=body.decision,
            comments=body.comments,
            scores=body.scores,
        )
    except editorial.EditorialError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    audit.record(db,
                 action=(AuditAction.APPROVE if body.decision in ("approve", "publish")
                         else AuditAction.UPDATE),
                 tenant_id=tenant_id, actor_id=principal.id, entity_type="question",
                 entity_id=question.id,
                 summary=f"Question {body.decision}: {review.from_status} -> {review.to_status}",
                 **meta)
    return {
        "question_id": question.id,
        "from_status": review.from_status,
        "to_status": review.to_status,
        "is_servable": question.is_servable,
        "provenance": editorial.ai_content_disclosure(question),
    }


@router.patch("/questions/{question_id}")
def edit_question(
    question_id: str,
    body: QuestionEditIn,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
) -> dict[str, Any]:
    """Edit an item. A published item returns to review afterwards."""
    principal.require("exam.bank.manage")
    question = db.get(Question, question_id)
    if question is None or question.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Question not found.")
    try:
        editorial.edit_question(
            db,
            question,
            editor_id=principal.id,
            changes=body.changes,
            change_summary=body.change_summary,
        )
    except editorial.EditorialError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    audit.record(db, action=AuditAction.UPDATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="question", entity_id=question.id,
                 summary=f"Edited to version {question.version}: {body.change_summary}",
                 **meta)
    return {
        "question_id": question.id,
        "version": question.version,
        "editorial_status": question.editorial_status,
        "provenance": editorial.ai_content_disclosure(question),
    }


# ==========================================================================
# Generation
# ==========================================================================
@router.get("/generation/provider")
def generation_provider(principal: CurrentPrincipal) -> dict[str, Any]:
    """Which engine will produce content, and whether it is the placeholder."""
    principal.require("exam.question.generate")
    return describe_provider(get_provider())


@router.post("/generation/jobs", status_code=status.HTTP_201_CREATED)
def create_generation_job(
    body: GenerationRequestIn,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
) -> dict[str, Any]:
    """Queue a generation job, optionally running it inline.

    ``run_now`` is honest about its limits: it runs the pipeline inside the
    request, which is fine for a handful of items and wrong for fifty. The
    twenty-minute service level assumes the worker.
    """
    principal.require("exam.question.generate")
    job = pipeline.create_job(
        db,
        tenant_id=tenant_id,
        bank_id=body.bank_id,
        topics=body.topics,
        learning_objectives=body.learning_objectives,
        count=body.count,
        org_unit_id=body.org_unit_id,
        specialty_id=body.specialty_id,
        training_level=body.training_level,
        requested_by_id=principal.id,
        target_user_id=body.target_user_id,
        trigger=body.trigger,
        blueprint=body.blueprint,
        difficulty_mix=body.difficulty_mix,
        requires_review=body.requires_review,
    )
    audit.record(db, action=AuditAction.CREATE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="generation_job", entity_id=job.id,
                 summary=f"Requested {job.requested_count} items on {', '.join(job.topics) or 'general'}",
                 **meta)

    payload: dict[str, Any] = {
        "job_id": job.id,
        "stage": job.stage,
        "requested_count": job.requested_count,
        "deadline_minutes": job.deadline_minutes,
        "provider": describe_provider(get_provider()),
    }
    if body.run_now:
        try:
            result = pipeline.run_job(db, job)
        except pipeline.PipelineError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        payload["result"] = result.summary
    return payload


@router.post("/generation/jobs/{job_id}/run")
def run_generation_job(
    job_id: str, db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId
) -> dict[str, Any]:
    """Execute a queued job now."""
    principal.require("exam.question.generate")
    job = db.get(GenerationJob, job_id)
    if job is None or job.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
    if job.stage not in (GenerationStage.QUEUED, GenerationStage.FAILED):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"This job is already at '{job.stage}'."
        )
    try:
        result = pipeline.run_job(db, job)
    except pipeline.PipelineError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return result.summary


@router.get("/generation/jobs/{job_id}")
def get_generation_job(
    job_id: str, db: DbSession, principal: CurrentPrincipal, tenant_id: TenantId
) -> dict[str, Any]:
    """Job status, including where a stalled run stopped and what it cost."""
    principal.require("exam.question.generate")
    job = db.get(GenerationJob, job_id)
    if job is None or job.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
    return {
        **job.as_dict(),
        "elapsed_seconds": job.elapsed_seconds,
        "met_deadline": job.met_deadline,
        "awaiting_review": editorial.queue_summary(db, tenant_id=tenant_id)[
            "unreviewed_ai_generated"
        ],
    }


@router.get("/generation/jobs/{job_id}/drafts")
def get_job_drafts(
    job_id: str,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    rejected_only: bool = Query(False),
) -> list[dict[str, Any]]:
    """Every draft the job produced, accepted and rejected alike.

    The rejected ones are the useful half when tuning a prompt: each carries
    the specific checks it failed.
    """
    principal.require("exam.question.generate")
    job = db.get(GenerationJob, job_id)
    if job is None or job.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
    drafts = job.drafts
    if rejected_only:
        drafts = [d for d in drafts if not d.is_accepted]
    return [d.as_dict() for d in drafts]


@router.post("/generation/jobs/{job_id}/release")
def release_generated_paper(
    job_id: str,
    body: ReleaseIn,
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    meta: ClientMeta,
) -> dict[str, Any]:
    """Create a paper from this job's approved items.

    Refuses while items remain unreviewed. That refusal is the editorial gate
    working, not an error to route around.
    """
    principal.require("exam.paper.manage")
    job = db.get(GenerationJob, job_id)
    if job is None or job.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")

    now = utcnow()
    year, week, _ = now.isocalendar()
    try:
        paper = pipeline.release_paper(
            db,
            job,
            released_by_id=principal.id,
            name=body.name,
            duration_minutes=body.duration_minutes,
            pass_mark_percent=body.pass_mark_percent,
            integrity_policy_id=body.integrity_policy_id,
            cycle_year=year,
            cycle_week=week,
            now=now,
        )
    except pipeline.PipelineError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    audit.record(db, action=AuditAction.APPROVE, tenant_id=tenant_id, actor_id=principal.id,
                 entity_type="exam_paper", entity_id=paper.id,
                 summary=f"Released '{paper.name}' from job {job.id}", **meta)
    return {
        "paper_id": paper.id,
        "name": paper.name,
        "question_count": paper.question_count,
        "cycle_year": paper.cycle_year,
        "cycle_week": paper.cycle_week,
        "is_published": paper.is_published,
    }


@router.get("/attempts")
def list_attempts(
    db: DbSession,
    principal: CurrentPrincipal,
    tenant_id: TenantId,
    user_id: str | None = None,
    paper_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    """A candidate's sittings. Defaults to the caller's own."""
    target = user_id or principal.id
    if target != principal.id:
        principal.require("exam.result.read.any")

    stmt = (
        select(ExamAttempt)
        .where(ExamAttempt.tenant_id == tenant_id, ExamAttempt.user_id == target)
        .order_by(ExamAttempt.started_at.desc())
        .limit(limit)
    )
    if paper_id:
        stmt = stmt.where(ExamAttempt.paper_id == paper_id)

    return [
        {
            "attempt_id": a.id,
            "paper_id": a.paper_id,
            "attempt_number": a.attempt_number,
            "status": a.status,
            "started_at": a.started_at.isoformat(),
            "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
            "percent_score": a.percent_score,
            "is_pass": a.is_pass,
            "cohort_percentile": a.cohort_percentile,
            "integrity_outcome": a.integrity_outcome,
            "in_progress": a.status == AttemptStatus.IN_PROGRESS,
        }
        for a in db.execute(stmt).scalars()
    ]
