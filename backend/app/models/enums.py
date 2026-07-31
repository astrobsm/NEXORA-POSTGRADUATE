"""Domain enumerations.

These are *vocabularies*, not policy. Anything an institution might reasonably want to
extend (specialties, activity types, requirement kinds) is either a database row or a
free-text code column with these values acting as well-known defaults.
"""

from __future__ import annotations

from enum import StrEnum


# --------------------------------------------------------------------------
# Tenancy
# --------------------------------------------------------------------------
class OrgKind(StrEnum):
    NATIONAL = "national"
    COLLEGE = "college"
    HOSPITAL = "hospital"
    FACULTY = "faculty"
    DEPARTMENT = "department"
    UNIT = "unit"
    SUBSPECIALTY = "subspecialty"
    PROGRAMME = "programme"


class AccreditingBody(StrEnum):
    NPMCN = "npmcn"
    WACS = "wacs"
    WACP = "wacp"
    MDCN = "mdcn"
    NUC = "nuc"
    ROYAL_COLLEGE = "royal_college"
    CUSTOM = "custom"


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------
class UserStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class Discipline(StrEnum):
    MEDICAL = "medical"
    DENTAL = "dental"
    ALLIED = "allied"


# --------------------------------------------------------------------------
# Curriculum
# --------------------------------------------------------------------------
class ProgrammeType(StrEnum):
    HOUSEMANSHIP = "housemanship"
    INTERNSHIP = "internship"
    RESIDENCY_JUNIOR = "residency_junior"      # Registrar / Part I
    RESIDENCY_SENIOR = "residency_senior"      # Senior Registrar / Part II
    FELLOWSHIP = "fellowship"
    SUBSPECIALTY_FELLOWSHIP = "subspecialty_fellowship"
    CME = "cme"


class TrainingLevel(StrEnum):
    HOUSE_OFFICER = "house_officer"
    INTERN = "intern"
    MEDICAL_OFFICER = "medical_officer"
    REGISTRAR = "registrar"
    SENIOR_REGISTRAR = "senior_registrar"
    FELLOW = "fellow"
    CONSULTANT_TRAINER = "consultant_trainer"


class CurriculumStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class CompetencyDomain(StrEnum):
    PATIENT_CARE = "patient_care"
    MEDICAL_KNOWLEDGE = "medical_knowledge"
    PROCEDURAL_SKILL = "procedural_skill"
    COMMUNICATION = "communication"
    PROFESSIONALISM = "professionalism"
    PRACTICE_BASED_LEARNING = "practice_based_learning"
    SYSTEMS_BASED_PRACTICE = "systems_based_practice"
    LEADERSHIP = "leadership"
    TEACHING = "teaching"
    RESEARCH = "research"


class EntrustmentLevel(StrEnum):
    """Entrustable Professional Activity supervision scale (Chen/ten Cate)."""

    OBSERVE_ONLY = "1_observe_only"
    DIRECT_SUPERVISION = "2_direct_supervision"
    INDIRECT_SUPERVISION = "3_indirect_supervision"
    INDEPENDENT = "4_independent"
    SUPERVISE_OTHERS = "5_supervise_others"


ENTRUSTMENT_ORDER: dict[str, int] = {
    EntrustmentLevel.OBSERVE_ONLY: 1,
    EntrustmentLevel.DIRECT_SUPERVISION: 2,
    EntrustmentLevel.INDIRECT_SUPERVISION: 3,
    EntrustmentLevel.INDEPENDENT: 4,
    EntrustmentLevel.SUPERVISE_OTHERS: 5,
}


# --------------------------------------------------------------------------
# Configurable requirement rules — the heart of "no code change for policy"
# --------------------------------------------------------------------------
class RequirementKind(StrEnum):
    PROCEDURE_COUNT = "procedure_count"
    PROCEDURE_ROLE_COUNT = "procedure_role_count"
    LOGBOOK_ENTRY_COUNT = "logbook_entry_count"
    CLINIC_COUNT = "clinic_count"
    WARD_ROUND_COUNT = "ward_round_count"
    COMPETENCY_LEVEL = "competency_level"
    EPA_LEVEL = "epa_level"
    ACADEMIC_ATTENDANCE_PCT = "academic_attendance_pct"
    DUTY_ATTENDANCE_PCT = "duty_attendance_pct"
    ACTIVITY_PRESENTATION_COUNT = "activity_presentation_count"
    ASSESSMENT_PASS_COUNT = "assessment_pass_count"
    ASSESSMENT_MEAN_SCORE = "assessment_mean_score"
    EXAM_PASS = "exam_pass"
    CME_CREDITS = "cme_credits"
    RESEARCH_OUTPUT = "research_output"
    PUBLICATION_COUNT = "publication_count"
    DISSERTATION_STAGE = "dissertation_stage"
    ROTATION_COMPLETION = "rotation_completion"
    TEACHING_HOURS = "teaching_hours"
    CUSTOM_EXPRESSION = "custom_expression"


class RequirementOperator(StrEnum):
    GTE = "gte"
    GT = "gt"
    LTE = "lte"
    LT = "lt"
    EQ = "eq"
    NEQ = "neq"


class RequirementScope(StrEnum):
    ROTATION = "rotation"
    TRAINING_YEAR = "training_year"
    PROGRAMME = "programme"
    PROMOTION = "promotion"
    EXAM_ELIGIBILITY = "exam_eligibility"
    ACCREDITATION = "accreditation"


class RequirementSeverity(StrEnum):
    MANDATORY = "mandatory"       # blocks progression
    RECOMMENDED = "recommended"   # scored but non-blocking
    INFORMATIONAL = "informational"


# --------------------------------------------------------------------------
# Training lifecycle
# --------------------------------------------------------------------------
class EnrolmentStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    ON_LEAVE = "on_leave"
    SUSPENDED = "suspended"
    WITHDRAWN = "withdrawn"
    TRANSFERRED = "transferred"
    COMPLETED = "completed"


class RotationStatus(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"
    EXTENDED = "extended"
    REMEDIAL = "remedial"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    FAILED = "failed"


class LeaveType(StrEnum):
    ANNUAL = "annual"
    SICK = "sick"
    MATERNITY = "maternity"
    PATERNITY = "paternity"
    STUDY = "study"
    EXAMINATION = "examination"
    COMPASSIONATE = "compassionate"
    SABBATICAL = "sabbatical"
    UNPAID = "unpaid"


class ApprovalStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETURNED = "returned"
    WITHDRAWN = "withdrawn"


# --------------------------------------------------------------------------
# Duty
# --------------------------------------------------------------------------
class DutyKind(StrEnum):
    WARD = "ward"
    EMERGENCY = "emergency"
    THEATRE = "theatre"
    LABOUR_WARD = "labour_ward"
    ICU = "icu"
    CLINIC = "clinic"
    CALL = "call"
    NIGHT_CALL = "night_call"
    WEEKEND = "weekend"
    PUBLIC_HOLIDAY = "public_holiday"
    ADMIN = "admin"


class ShiftStatus(StrEnum):
    SCHEDULED = "scheduled"
    SWAPPED = "swapped"
    COVERED = "covered"
    COMPLETED = "completed"
    MISSED = "missed"
    CANCELLED = "cancelled"


class AttendanceStatus(StrEnum):
    PRESENT = "present"
    LATE = "late"
    PARTIAL = "partial"
    ABSENT = "absent"
    EXCUSED = "excused"


# --------------------------------------------------------------------------
# Logbook
# --------------------------------------------------------------------------
class LogEntryType(StrEnum):
    ADMISSION = "admission"
    DISCHARGE = "discharge"
    WARD_ROUND = "ward_round"
    CLINIC = "clinic"
    MAJOR_PROCEDURE = "major_procedure"
    MINOR_PROCEDURE = "minor_procedure"
    EMERGENCY_CALL = "emergency_call"
    CONSULTATION = "consultation"
    TEACHING = "teaching"
    RESEARCH_ACTIVITY = "research_activity"
    SIMULATION = "simulation"
    SKILL_PRACTICE = "skill_practice"
    DEATH = "death"
    COMPLICATION = "complication"


class ParticipationRole(StrEnum):
    OBSERVED = "observed"
    ASSISTED = "assisted"
    PERFORMED_SUPERVISED = "performed_supervised"
    PERFORMED_INDEPENDENT = "performed_independent"
    SUPERVISED_OTHER = "supervised_other"


PARTICIPATION_WEIGHT: dict[str, float] = {
    ParticipationRole.OBSERVED: 0.25,
    ParticipationRole.ASSISTED: 0.5,
    ParticipationRole.PERFORMED_SUPERVISED: 1.0,
    ParticipationRole.PERFORMED_INDEPENDENT: 1.25,
    ParticipationRole.SUPERVISED_OTHER: 1.5,
}


class CaseComplexity(StrEnum):
    ROUTINE = "routine"
    INTERMEDIATE = "intermediate"
    COMPLEX = "complex"
    HIGHLY_COMPLEX = "highly_complex"


class CaseOutcome(StrEnum):
    UNEVENTFUL = "uneventful"
    MINOR_COMPLICATION = "minor_complication"
    MAJOR_COMPLICATION = "major_complication"
    MORTALITY = "mortality"
    UNKNOWN = "unknown"


class ValidationStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    VALIDATED = "validated"
    QUERIED = "queried"
    REJECTED = "rejected"


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------
class AssessmentKind(StrEnum):
    MINI_CEX = "mini_cex"
    DOPS = "dops"
    CBD = "cbd"
    MSF = "msf"
    OSATS = "osats"
    OSCE = "osce"
    LOGBOOK_REVIEW = "logbook_review"
    ROTATION_END = "rotation_end"
    ANNUAL_REVIEW = "annual_review"
    PROFESSIONALISM = "professionalism"
    TEACHING_OBSERVATION = "teaching_observation"
    EXIT_INTERVIEW = "exit_interview"
    CUSTOM = "custom"


class AssessmentVerdict(StrEnum):
    BELOW_EXPECTATION = "below_expectation"
    BORDERLINE = "borderline"
    MEETS_EXPECTATION = "meets_expectation"
    ABOVE_EXPECTATION = "above_expectation"
    OUTSTANDING = "outstanding"


VERDICT_SCORE: dict[str, float] = {
    AssessmentVerdict.BELOW_EXPECTATION: 30.0,
    AssessmentVerdict.BORDERLINE: 50.0,
    AssessmentVerdict.MEETS_EXPECTATION: 70.0,
    AssessmentVerdict.ABOVE_EXPECTATION: 85.0,
    AssessmentVerdict.OUTSTANDING: 95.0,
}


# --------------------------------------------------------------------------
# Academic activities
# --------------------------------------------------------------------------
class AcademicActivityKind(StrEnum):
    GRAND_ROUND = "grand_round"
    WARD_ROUND = "ward_round"
    CONSULTANT_ROUND = "consultant_round"
    TEACHING_ROUND = "teaching_round"
    MORNING_REVIEW = "morning_review"
    JOURNAL_CLUB = "journal_club"
    SEMINAR = "seminar"
    MORTALITY_MEETING = "mortality_meeting"
    MORBIDITY_MEETING = "morbidity_meeting"
    CPC = "clinicopathological_conference"
    TUMOUR_BOARD = "tumour_board"
    RESEARCH_MEETING = "research_meeting"
    RADIOLOGY_MEETING = "radiology_meeting"
    PATHOLOGY_MEETING = "pathology_meeting"
    SKILL_LAB = "skill_lab"
    SIMULATION = "simulation"
    SKILLS_WORKSHOP = "skills_workshop"
    NATIONAL_CONFERENCE = "national_conference"
    INTERNATIONAL_CONFERENCE = "international_conference"
    GUEST_LECTURE = "guest_lecture"
    COURSE = "course"
    WEBINAR = "webinar"
    CME_SESSION = "cme_session"


class ParticipantRole(StrEnum):
    ATTENDEE = "attendee"
    PRESENTER = "presenter"
    MODERATOR = "moderator"
    DISCUSSANT = "discussant"
    EXAMINER = "examiner"
    ORGANISER = "organiser"


# --------------------------------------------------------------------------
# CBT / examinations
# --------------------------------------------------------------------------
class QuestionType(StrEnum):
    SINGLE_BEST_ANSWER = "single_best_answer"
    MULTIPLE_TRUE_FALSE = "multiple_true_false"
    EXTENDED_MATCHING = "extended_matching"
    SHORT_ANSWER = "short_answer"
    IMAGE_BASED = "image_based"
    VIDEO_BASED = "video_based"
    OSCE_STATION = "osce_station"
    CLINICAL_CASE = "clinical_case"


class MediaKind(StrEnum):
    NONE = "none"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    RADIOLOGY = "radiology"
    HISTOLOGY = "histology"
    PATHOLOGY = "pathology"
    ECG = "ecg"


class ExamMode(StrEnum):
    PRACTICE = "practice"
    TIMED = "timed"
    MOCK = "mock"
    FORMATIVE = "formative"
    SUMMATIVE = "summative"
    ADAPTIVE = "adaptive"


class AttemptStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    MARKED = "marked"
    ABANDONED = "abandoned"
    VOIDED = "voided"


# --------------------------------------------------------------------------
# CME
# --------------------------------------------------------------------------
class CmeResourceKind(StrEnum):
    GUIDELINE = "guideline"
    JOURNAL_ARTICLE = "journal_article"
    VIDEO = "video"
    BOOK = "book"
    BOOK_CHAPTER = "book_chapter"
    PODCAST = "podcast"
    CLINICAL_UPDATE = "clinical_update"
    EVIDENCE_SUMMARY = "evidence_summary"
    COURSE = "course"


class CmeStatus(StrEnum):
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ASSESSED = "assessed"
    OVERDUE = "overdue"
    WAIVED = "waived"


# --------------------------------------------------------------------------
# Research & dissertation
# --------------------------------------------------------------------------
class ResearchType(StrEnum):
    DISSERTATION = "dissertation"
    THESIS = "thesis"
    AUDIT = "audit"
    CASE_REPORT = "case_report"
    CASE_SERIES = "case_series"
    ORIGINAL_RESEARCH = "original_research"
    SYSTEMATIC_REVIEW = "systematic_review"
    QUALITY_IMPROVEMENT = "quality_improvement"


class DissertationStage(StrEnum):
    CONCEPT = "concept"
    SUPERVISOR_ASSIGNMENT = "supervisor_assignment"
    TOPIC_APPROVAL = "topic_approval"
    PROPOSAL_WRITING = "proposal_writing"
    PROPOSAL_DEFENCE = "proposal_defence"
    ETHICS_APPROVAL = "ethics_approval"
    DATA_COLLECTION = "data_collection"
    ANALYSIS = "analysis"
    DRAFT_SUBMISSION = "draft_submission"
    CORRECTIONS = "corrections"
    FINAL_DEFENCE = "final_defence"
    COLLEGE_SUBMISSION = "college_submission"
    PUBLICATION = "publication"
    COMPLETED = "completed"


DISSERTATION_STAGE_ORDER: tuple[str, ...] = tuple(s.value for s in DissertationStage)


class PublicationType(StrEnum):
    JOURNAL_ARTICLE = "journal_article"
    CONFERENCE_ABSTRACT = "conference_abstract"
    BOOK_CHAPTER = "book_chapter"
    POSTER = "poster"
    ORAL_PRESENTATION = "oral_presentation"
    PREPRINT = "preprint"


# --------------------------------------------------------------------------
# Analytics & promotion
# --------------------------------------------------------------------------
class ScoreDomain(StrEnum):
    CLINICAL_COMPETENCY = "clinical_competency"
    RESEARCH = "research"
    ACADEMIC = "academic"
    PROFESSIONALISM = "professionalism"
    LEADERSHIP = "leadership"
    ATTENDANCE = "attendance"
    TEACHING = "teaching"
    EXAM_READINESS = "exam_readiness"


class RagStatus(StrEnum):
    """Colour-coded dashboard status."""

    RED = "red"
    AMBER = "amber"
    GREEN = "green"
    UNKNOWN = "unknown"


class PromotionOutcome(StrEnum):
    RECOMMENDED = "recommended"
    NOT_RECOMMENDED = "not_recommended"
    DEFERRED = "deferred"
    APPROVED = "approved"
    DECLINED = "declined"
    CONDITIONAL = "conditional"


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
class NotificationChannel(StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    WEBHOOK = "webhook"


class NotificationPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------
class AuditAction(StrEnum):
    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    EXPORT = "export"
    VALIDATE = "validate"
    APPROVE = "approve"
    REJECT = "reject"
    SYNC = "sync"
    CONFIG_CHANGE = "config_change"
