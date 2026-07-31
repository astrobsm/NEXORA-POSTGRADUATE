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
from app.models.branding import BrandingAsset, BrandingAssetKind
from app.models.assessment import (
    Assessment,
    AssessmentTemplate,
    CompetencyRating,
    MultiSourceFeedbackRound,
)
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
    "Base",
    # tenancy
    "Tenant",
    "OrgUnit",
    "TenantIntegration",
    "BrandingAsset",
    "BrandingAssetKind",
    # identity
    "User",
    "Role",
    "Permission",
    "RoleAssignment",
    "SupervisorProfile",
    "UserSession",
    # curriculum
    "Specialty",
    "Programme",
    "CurriculumVersion",
    "TrainingYear",
    "RotationTemplate",
    "Competency",
    "RequirementRule",
    "ProcedureCatalogueItem",
    # training
    "Enrolment",
    "RotationAssignment",
    "LeaveRecord",
    "TransferRecord",
    # duty
    "DutyRoster",
    "DutyShift",
    "DutySwapRequest",
    "AttendanceRecord",
    # logbook
    "LogEntry",
    "LogEntryAudit",
    "TeachingRecord",
    "log_entry_competencies",
    # assessment
    "AssessmentTemplate",
    "Assessment",
    "CompetencyRating",
    "MultiSourceFeedbackRound",
    # academic
    "AcademicActivity",
    "ActivityParticipant",
    "ConferenceRecord",
    # cbt
    "QuestionBank",
    "Question",
    "ExamPaper",
    "ExamAttempt",
    "ExamResponse",
    # cme
    "CmeResource",
    "CmeAssignment",
    "CmeCreditLedger",
    # research
    "ResearchProject",
    "ProjectSupervision",
    "DissertationMilestone",
    "SupervisionMeeting",
    "Publication",
    # analytics
    "ScoreSnapshot",
    "PromotionReview",
    "AccreditationProfile",
    "AccreditationCriterion",
    "AccreditationReview",
    "AccreditationEvidence",
    # system
    "NotificationTemplate",
    "NotificationRule",
    "Notification",
    "AuditLog",
    "SyncCheckpoint",
    "SyncConflict",
    "GeneratedReport",
]
