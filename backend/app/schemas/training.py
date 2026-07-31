"""Tenancy, curriculum, training, logbook, assessment and academic contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ApiModel


# --------------------------------------------------------------------------
# Tenancy
# --------------------------------------------------------------------------
class TenantOut(ApiModel):
    id: str
    name: str
    code: str
    slug: str
    kind: str
    country: str
    state: str | None = None
    city: str | None = None
    timezone: str
    accrediting_bodies: list[str]
    branding: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    is_active: bool


class OrgUnitOut(ApiModel):
    id: str
    tenant_id: str
    parent_id: str | None = None
    kind: str
    name: str
    short_name: str | None = None
    code: str
    path: str
    depth: int
    discipline: str
    specialty_id: str | None = None
    head_user_id: str | None = None
    capacity: dict[str, Any] = Field(default_factory=dict)
    is_active: bool


class OrgUnitTree(OrgUnitOut):
    children: list[OrgUnitTree] = Field(default_factory=list)


class OrgUnitCreate(BaseModel):
    parent_id: str | None = None
    kind: str
    name: str
    code: str
    short_name: str | None = None
    discipline: str = "medical"
    specialty_id: str | None = None
    head_user_id: str | None = None
    capacity: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None


# --------------------------------------------------------------------------
# Curriculum
# --------------------------------------------------------------------------
class SpecialtyOut(ApiModel):
    id: str
    tenant_id: str | None = None
    parent_id: str | None = None
    code: str
    name: str
    faculty_group: str | None = None
    discipline: str
    recognised_by: list[str]
    is_subspecialty: bool
    is_active: bool


class SpecialtyCreate(BaseModel):
    code: str
    name: str
    parent_id: str | None = None
    faculty_group: str | None = None
    discipline: str = "medical"
    recognised_by: list[str] = Field(default_factory=list)
    description: str | None = None


class RequirementRuleOut(ApiModel):
    id: str
    code: str | None = None
    label: str
    kind: str
    operator: str
    target_value: float
    parameters: dict[str, Any] = Field(default_factory=dict)
    scope: str
    severity: str
    weight: float
    score_domain: str | None = None
    guidance: str | None = None
    source_reference: str | None = None
    training_year_id: str | None = None
    rotation_template_id: str | None = None
    competency_id: str | None = None
    is_active: bool


class RequirementRuleCreate(BaseModel):
    label: str
    kind: str
    operator: str = "gte"
    target_value: float = 0.0
    parameters: dict[str, Any] = Field(default_factory=dict)
    scope: str = "programme"
    severity: str = "mandatory"
    weight: float = 1.0
    score_domain: str | None = None
    code: str | None = None
    guidance: str | None = None
    source_reference: str | None = None
    training_year_id: str | None = None
    rotation_template_id: str | None = None
    competency_id: str | None = None


class CompetencyOut(ApiModel):
    id: str
    code: str
    title: str
    description: str | None = None
    domain: str
    is_epa: bool
    target_by_year: dict[str, Any] = Field(default_factory=dict)
    exit_target: str
    weight: float
    assessment_methods: list[str]
    parent_id: str | None = None


class RotationTemplateOut(ApiModel):
    id: str
    training_year_id: str
    org_unit_id: str | None = None
    name: str
    code: str | None = None
    sequence: int
    duration_weeks: int
    is_elective: bool
    is_mandatory: bool
    max_trainees: int | None = None
    objectives: list[str]
    required_assessments: list[str]


class TrainingYearOut(ApiModel):
    id: str
    sequence: int
    name: str
    level: str
    duration_months: int
    objectives: list[str]
    expectations: dict[str, Any] = Field(default_factory=dict)
    rotations: list[RotationTemplateOut] = Field(default_factory=list)


class CurriculumVersionOut(ApiModel):
    id: str
    programme_id: str
    version: str
    title: str
    status: str
    effective_from: date | None = None
    aims: str | None = None
    score_weights: dict[str, Any] = Field(default_factory=dict)
    training_years: list[TrainingYearOut] = Field(default_factory=list)
    competencies: list[CompetencyOut] = Field(default_factory=list)
    requirements: list[RequirementRuleOut] = Field(default_factory=list)


class ProgrammeOut(ApiModel):
    id: str
    tenant_id: str
    org_unit_id: str
    specialty_id: str | None = None
    code: str
    name: str
    programme_type: str
    entry_level: str
    exit_level: str
    awarding_body: str | None = None
    awarding_body_name: str | None = None
    duration_months: int
    annual_intake: int
    is_active: bool


class ProgrammeCreate(BaseModel):
    org_unit_id: str
    code: str
    name: str
    programme_type: str = "residency_junior"
    specialty_id: str | None = None
    entry_level: str = "registrar"
    exit_level: str = "senior_registrar"
    awarding_body: str | None = None
    awarding_body_name: str | None = None
    duration_months: int = 48
    annual_intake: int = 6
    description: str | None = None


class TrainingYearCreate(BaseModel):
    sequence: int
    name: str
    level: str = "registrar"
    duration_months: int = 12
    objectives: list[str] = Field(default_factory=list)
    expectations: dict[str, Any] = Field(default_factory=dict)


class RotationTemplateCreate(BaseModel):
    training_year_id: str
    name: str
    sequence: int = 1
    duration_weeks: int = 12
    org_unit_id: str | None = None
    code: str | None = None
    is_elective: bool = False
    is_mandatory: bool = True
    max_trainees: int | None = None
    objectives: list[str] = Field(default_factory=list)
    required_assessments: list[str] = Field(default_factory=list)


class CompetencyCreate(BaseModel):
    code: str
    title: str
    domain: str = "patient_care"
    description: str | None = None
    is_epa: bool = False
    target_by_year: dict[str, Any] = Field(default_factory=dict)
    exit_target: str = "4_independent"
    weight: float = 1.0
    assessment_methods: list[str] = Field(default_factory=list)
    parent_id: str | None = None


# --------------------------------------------------------------------------
# Enrolment & rotations
# --------------------------------------------------------------------------
class EnrolmentOut(ApiModel):
    id: str
    trainee_id: str
    trainee_name: str | None = None
    programme_id: str
    programme_name: str | None = None
    curriculum_version_id: str
    org_unit_id: str
    org_unit_name: str | None = None
    primary_supervisor_id: str | None = None
    primary_supervisor_name: str | None = None
    cohort_year: int
    current_level: str
    current_year: int
    status: str
    start_date: date
    expected_end_date: date
    actual_end_date: date | None = None
    interruption_days: int
    latest_overall_score: float | None = None
    latest_rag: str | None = None
    promotion_ready: bool
    last_scored_at: datetime | None = None


class EnrolmentCreate(BaseModel):
    trainee_id: str
    programme_id: str
    org_unit_id: str | None = None
    curriculum_version_id: str | None = None
    primary_supervisor_id: str | None = None
    cohort_year: int | None = None
    current_level: str | None = None
    start_date: date
    generate_rotations: bool = True


class RotationAssignmentOut(ApiModel):
    id: str
    enrolment_id: str
    rotation_template_id: str | None = None
    org_unit_id: str
    org_unit_name: str | None = None
    supervisor_id: str | None = None
    supervisor_name: str | None = None
    name: str
    training_year: int
    sequence: int
    start_date: date
    end_date: date
    status: str
    is_elective: bool
    is_remedial: bool
    completion_percent: float
    supervisor_comment: str | None = None


class RotationPlanOut(BaseModel):
    enrolment_id: str
    planned: list[dict[str, Any]]
    capacity_warnings: list[dict[str, Any]]


class RotationCloseRequest(BaseModel):
    outcome: str = "completed"
    comment: str | None = None
    force: bool = False


class RotationExtendRequest(BaseModel):
    new_end_date: date
    reason: str
    cascade: bool = True


class LeaveCreate(BaseModel):
    leave_type: str = "annual"
    start_date: date
    end_date: date
    reason: str | None = None
    extends_training: bool = False


class LeaveOut(ApiModel):
    id: str
    enrolment_id: str
    leave_type: str
    start_date: date
    end_date: date
    extends_training: bool
    reason: str | None = None
    status: str
    approver_id: str | None = None
    decided_at: datetime | None = None


# --------------------------------------------------------------------------
# Logbook
# --------------------------------------------------------------------------
class LogEntryCreate(BaseModel):
    entry_type: str
    occurred_at: datetime
    title: str
    summary: str | None = None
    rotation_assignment_id: str | None = None
    org_unit_id: str | None = None
    patient_reference: str | None = None
    patient_age_years: int | None = None
    patient_age_months: int | None = None
    patient_sex: str | None = None
    setting: str | None = None
    diagnosis: str | None = None
    diagnosis_codes: dict[str, Any] = Field(default_factory=dict)
    procedure_id: str | None = None
    procedure_name: str | None = None
    procedure_grade: str | None = None
    participation_role: str | None = None
    complexity: str = "routine"
    outcome: str = "unknown"
    complication_detail: str | None = None
    anaesthesia_type: str | None = None
    duration_minutes: int | None = None
    quantity: int = 1
    reflection: str | None = None
    learning_points: list[str] = Field(default_factory=list)
    competency_ids: list[str] = Field(default_factory=list)
    supervisor_id: str | None = None
    attachment_keys: list[str] = Field(default_factory=list)
    client_uuid: str | None = None
    captured_offline: bool = False


class LogEntryUpdate(BaseModel):
    title: str | None = None
    summary: str | None = None
    diagnosis: str | None = None
    procedure_name: str | None = None
    procedure_grade: str | None = None
    participation_role: str | None = None
    complexity: str | None = None
    outcome: str | None = None
    complication_detail: str | None = None
    duration_minutes: int | None = None
    quantity: int | None = None
    reflection: str | None = None
    learning_points: list[str] | None = None
    supervisor_id: str | None = None
    competency_ids: list[str] | None = None


class LogEntryOut(ApiModel):
    id: str
    enrolment_id: str
    rotation_assignment_id: str | None = None
    entry_type: str
    occurred_at: datetime
    occurred_on: date
    title: str
    summary: str | None = None
    patient_reference: str | None = None
    patient_age_years: int | None = None
    patient_sex: str | None = None
    setting: str | None = None
    diagnosis: str | None = None
    procedure_name: str | None = None
    procedure_grade: str | None = None
    participation_role: str | None = None
    complexity: str
    outcome: str
    duration_minutes: int | None = None
    quantity: int
    reflection: str | None = None
    learning_points: list[str]
    supervisor_id: str | None = None
    supervisor_name: str | None = None
    validation_status: str
    validated_at: datetime | None = None
    validator_comment: str | None = None
    query_count: int
    captured_offline: bool
    revision: int
    client_uuid: str | None = None
    created_at: datetime
    updated_at: datetime


class LogValidationRequest(BaseModel):
    decision: str = Field(description="validated | queried | rejected")
    comment: str | None = None
    competency_ratings: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Optional entrustment ratings awarded alongside validation.",
    )


class LogbookSummary(BaseModel):
    total: int
    validated: int
    pending: int
    queried: int
    rejected: int
    by_type: dict[str, int]
    by_role: dict[str, int]
    by_month: dict[str, int]
    major_procedures: int
    minor_procedures: int


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------
class AssessmentTemplateOut(ApiModel):
    id: str
    code: str
    name: str
    kind: str
    description: str | None = None
    instructions: str | None = None
    form_schema: list[dict[str, Any]]
    scoring_config: dict[str, Any]
    competency_codes: list[str]
    requires_trainee_reflection: bool
    min_assessors: int
    applies_to_levels: list[str]
    is_active: bool


class AssessmentTemplateCreate(BaseModel):
    code: str
    name: str
    kind: str = "mini_cex"
    description: str | None = None
    instructions: str | None = None
    form_schema: list[dict[str, Any]] = Field(default_factory=list)
    scoring_config: dict[str, Any] = Field(default_factory=dict)
    competency_codes: list[str] = Field(default_factory=list)
    org_unit_id: str | None = None
    applies_to_levels: list[str] = Field(default_factory=list)


class AssessmentCreate(BaseModel):
    template_id: str
    enrolment_id: str
    occurred_on: date
    rotation_assignment_id: str | None = None
    setting: str | None = None
    case_summary: str | None = None
    case_complexity: str | None = None
    responses: dict[str, Any] = Field(default_factory=dict)
    strengths: str | None = None
    development_needs: str | None = None
    agreed_actions: str | None = None
    submit: bool = True
    competency_ratings: list[dict[str, Any]] = Field(default_factory=list)


class AssessmentOut(ApiModel):
    id: str
    template_id: str
    template_name: str | None = None
    template_kind: str | None = None
    enrolment_id: str
    rotation_assignment_id: str | None = None
    assessor_id: str | None = None
    assessor_name: str | None = None
    occurred_on: date
    setting: str | None = None
    case_summary: str | None = None
    responses: dict[str, Any]
    raw_score: float | None = None
    max_score: float | None = None
    percent_score: float | None = None
    verdict: str | None = None
    is_pass: bool | None = None
    strengths: str | None = None
    development_needs: str | None = None
    agreed_actions: str | None = None
    trainee_reflection: str | None = None
    status: str
    submitted_at: datetime | None = None
    created_at: datetime


class CompetencyRatingOut(ApiModel):
    id: str
    competency_id: str
    competency_code: str | None = None
    competency_title: str | None = None
    level: str
    level_value: int
    rated_on: date
    assessor_id: str | None = None
    evidence: str | None = None


# --------------------------------------------------------------------------
# Academic activities
# --------------------------------------------------------------------------
class AcademicActivityCreate(BaseModel):
    org_unit_id: str
    kind: str
    title: str
    scheduled_at: datetime
    duration_minutes: int = 60
    abstract: str | None = None
    venue: str | None = None
    is_virtual: bool = False
    meeting_url: str | None = None
    presenter_id: str | None = None
    moderator_id: str | None = None
    external_presenter: str | None = None
    expected_levels: list[str] = Field(default_factory=list)
    is_mandatory: bool = True
    cme_credits: float = 0.0
    series_code: str | None = None
    tags: list[str] = Field(default_factory=list)


class AcademicActivityOut(ApiModel):
    id: str
    org_unit_id: str
    kind: str
    title: str
    abstract: str | None = None
    scheduled_at: datetime
    scheduled_on: date
    duration_minutes: int
    venue: str | None = None
    is_virtual: bool
    presenter_id: str | None = None
    presenter_name: str | None = None
    external_presenter: str | None = None
    is_mandatory: bool
    cme_credits: float
    status: str
    checkin_code: str | None = None
    attendee_count: int = 0


class AttendanceMarkRequest(BaseModel):
    user_ids: list[str] = Field(default_factory=list)
    role: str = "attendee"
    attended: bool = True
    checkin_code: str | None = None
    minutes_present: int | None = None


class ActivityParticipantOut(ApiModel):
    id: str
    activity_id: str
    user_id: str
    user_name: str | None = None
    role: str
    attended: bool
    checked_in_at: datetime | None = None
    credits_awarded: float
