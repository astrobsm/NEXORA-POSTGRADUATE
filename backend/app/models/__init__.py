"""Domain model package.

Importing this module registers every mapper with the declarative ``Base``, which is
what Alembic autogeneration and ``Base.metadata.create_all`` rely on.
"""

from __future__ import annotations

from app.db.base import Base
from app.models.academic import (
    AcademicActivity,
    ActivityParticipant,
    ConferenceRecord,
)
from app.models.analytics import (
    AccreditationCriterion,
    AccreditationEvidence,
    AccreditationProfile,
    AccreditationReview,
    PromotionReview,
    ScoreSnapshot,
)
from app.models.assessment import (
    Assessment,
    AssessmentTemplate,
    CompetencyRating,
    MultiSourceFeedbackRound,
)
from app.models.branding import BrandingAsset, BrandingAssetKind
from app.models.cbt import (
    ExamAttempt,
    ExamPaper,
    ExamResponse,
    Question,
    QuestionBank,
)
from app.models.cme import CmeAssignment, CmeCreditLedger, CmeResource
from app.models.curriculum import (
    Competency,
    CurriculumVersion,
    ProcedureCatalogueItem,
    Programme,
    RequirementRule,
    RotationTemplate,
    Specialty,
    TrainingYear,
)
from app.models.duty import (
    AttendanceRecord,
    DutyRoster,
    DutyShift,
    DutySwapRequest,
)
from app.models.identity import (
    Permission,
    Role,
    RoleAssignment,
    SupervisorProfile,
    User,
    UserSession,
)
from app.models.learning import (
    EngagementSnapshot,
    ExamConsent,
    GenerationJob,
    IntegrityEvent,
    IntegrityPolicy,
    IntegrityReport,
    ItemAnalysis,
    LearningPlan,
    LearningPlanAction,
    PaperAnalysis,
    QuestionDraft,
    QuestionReview,
    QuestionVersion,
    ReadinessSnapshot,
    ReadingAnnotation,
    ReadingEvent,
    ReadingSession,
)
from app.models.logbook import (
    LogEntry,
    LogEntryAudit,
    TeachingRecord,
    log_entry_competencies,
)
from app.models.research import (
    DissertationMilestone,
    ProjectSupervision,
    Publication,
    ResearchProject,
    SupervisionMeeting,
)
from app.models.system import (
    AuditLog,
    GeneratedReport,
    Notification,
    NotificationRule,
    NotificationTemplate,
    SyncCheckpoint,
    SyncConflict,
)
from app.models.tenancy import OrgUnit, Tenant, TenantIntegration
from app.models.training import (
    Enrolment,
    LeaveRecord,
    RotationAssignment,
    TransferRecord,
)

__all__ = [
    "AcademicActivity",
    "AccreditationCriterion",
    "AccreditationEvidence",
    "AccreditationProfile",
    "AccreditationReview",
    "ActivityParticipant",
    "Assessment",
    "AssessmentTemplate",
    "AttendanceRecord",
    "AuditLog",
    "Base",
    "BrandingAsset",
    "BrandingAssetKind",
    "CmeAssignment",
    "CmeCreditLedger",
    "CmeResource",
    "Competency",
    "CompetencyRating",
    "ConferenceRecord",
    "CurriculumVersion",
    "DissertationMilestone",
    "DutyRoster",
    "DutyShift",
    "DutySwapRequest",
    "EngagementSnapshot",
    "Enrolment",
    "ExamAttempt",
    "ExamConsent",
    "ExamPaper",
    "ExamResponse",
    "GeneratedReport",
    "GenerationJob",
    "IntegrityEvent",
    "IntegrityPolicy",
    "IntegrityReport",
    "ItemAnalysis",
    "LearningPlan",
    "LearningPlanAction",
    "LeaveRecord",
    "LogEntry",
    "LogEntryAudit",
    "MultiSourceFeedbackRound",
    "Notification",
    "NotificationRule",
    "NotificationTemplate",
    "OrgUnit",
    "PaperAnalysis",
    "Permission",
    "ProcedureCatalogueItem",
    "Programme",
    "ProjectSupervision",
    "PromotionReview",
    "Publication",
    "Question",
    "QuestionBank",
    "QuestionDraft",
    "QuestionReview",
    "QuestionVersion",
    "ReadinessSnapshot",
    "ReadingAnnotation",
    "ReadingEvent",
    "ReadingSession",
    "RequirementRule",
    "ResearchProject",
    "Role",
    "RoleAssignment",
    "RotationAssignment",
    "RotationTemplate",
    "ScoreSnapshot",
    "Specialty",
    "SupervisionMeeting",
    "SupervisorProfile",
    "SyncCheckpoint",
    "SyncConflict",
    "TeachingRecord",
    "Tenant",
    "TenantIntegration",
    "TrainingYear",
    "TransferRecord",
    "User",
    "UserSession",
    "log_entry_competencies",
]
