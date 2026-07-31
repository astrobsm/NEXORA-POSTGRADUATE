"""Specialties, programmes, curriculum versions and the configurable requirement engine."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Date, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SoftDeleteMixin, SyncMixin, TimestampMixin
from app.models.enums import (
    CompetencyDomain,
    CurriculumStatus,
    Discipline,
    EntrustmentLevel,
    ProgrammeType,
    RequirementKind,
    RequirementOperator,
    RequirementScope,
    RequirementSeverity,
    TrainingLevel,
)

if TYPE_CHECKING:
    from app.models.tenancy import OrgUnit
    from app.models.training import Enrolment


class Specialty(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """A specialty or subspecialty.

    Self-referential so that *Plastic Surgery → Craniofacial Surgery* nests naturally,
    and creatable at runtime — adding a new specialty never requires a code change.
    ``tenant_id`` NULL means a platform-supplied specialty available to all institutions.
    """

    __tablename__ = "specialties"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_specialties_tenant_code"),
        Index("ix_specialties_parent", "parent_id"),
    )

    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), default=None, index=True
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("specialties.id", ondelete="SET NULL"), default=None
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Broad grouping used for navigation: Surgery, Medicine, Dentistry, ...
    faculty_group: Mapped[str | None] = mapped_column(String(120), default=None, index=True)
    discipline: Mapped[str] = mapped_column(String(16), default=Discipline.MEDICAL, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    #: College codes that recognise this specialty, e.g. ["npmcn", "wacs"].
    recognised_by: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    is_subspecialty: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)

    parent: Mapped[Specialty | None] = relationship(remote_side="Specialty.id", back_populates="children")
    children: Mapped[list[Specialty]] = relationship(back_populates="parent")


class Programme(Base, IdMixin, TimestampMixin, SoftDeleteMixin, SyncMixin):
    """A training programme run by a department: housemanship, residency, fellowship."""

    __tablename__ = "programmes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_programmes_tenant_code"),
        Index("ix_programmes_tenant_type", "tenant_id", "programme_type"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_unit_id: Mapped[str] = mapped_column(
        ForeignKey("org_units.id", ondelete="CASCADE"), nullable=False, index=True
    )
    specialty_id: Mapped[str | None] = mapped_column(
        ForeignKey("specialties.id", ondelete="SET NULL"), default=None, index=True
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    programme_type: Mapped[str] = mapped_column(
        String(32), default=ProgrammeType.RESIDENCY_JUNIOR, nullable=False
    )
    entry_level: Mapped[str] = mapped_column(String(32), default=TrainingLevel.REGISTRAR, nullable=False)
    exit_level: Mapped[str] = mapped_column(String(32), default=TrainingLevel.SENIOR_REGISTRAR, nullable=False)
    #: Awarding/accrediting body: npmcn | wacs | wacp | mdcn | royal_college | custom
    awarding_body: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    awarding_body_name: Mapped[str | None] = mapped_column(String(200), default=None)
    duration_months: Mapped[int] = mapped_column(default=48, nullable=False)
    annual_intake: Mapped[int] = mapped_column(default=6, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    org_unit: Mapped[OrgUnit] = relationship(back_populates="programmes")
    specialty: Mapped[Specialty | None] = relationship()
    versions: Mapped[list[CurriculumVersion]] = relationship(
        back_populates="programme", cascade="all, delete-orphan", passive_deletes=True
    )
    enrolments: Mapped[list[Enrolment]] = relationship(back_populates="programme", passive_deletes=True)

    @property
    def active_version(self) -> CurriculumVersion | None:
        return next((v for v in self.versions if v.status == CurriculumStatus.ACTIVE), None)


class CurriculumVersion(Base, IdMixin, TimestampMixin, SyncMixin):
    """An immutable-once-published snapshot of a programme's curriculum.

    Trainees are pinned to the version in force when they enrolled, so publishing a new
    curriculum never retroactively changes anyone's requirements.
    """

    __tablename__ = "curriculum_versions"
    __table_args__ = (
        UniqueConstraint("programme_id", "version", name="uq_curriculum_programme_version"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    programme_id: Mapped[str] = mapped_column(
        ForeignKey("programmes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=CurriculumStatus.DRAFT, nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date, default=None)
    effective_to: Mapped[date | None] = mapped_column(Date, default=None)
    approved_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    approved_on: Mapped[date | None] = mapped_column(Date, default=None)
    aims: Mapped[str | None] = mapped_column(Text, default=None)
    #: Domain weights used by the analytics engine, e.g.
    #: {"clinical_competency": 0.3, "academic": 0.2, ...}. Must sum to 1.0.
    score_weights: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: Recommended reading, courses, external resources.
    resources: Mapped[list[dict[str, Any]]] = mapped_column(default=list, nullable=False)
    change_notes: Mapped[str | None] = mapped_column(Text, default=None)

    programme: Mapped[Programme] = relationship(back_populates="versions")
    training_years: Mapped[list[TrainingYear]] = relationship(
        back_populates="curriculum_version",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TrainingYear.sequence",
    )
    competencies: Mapped[list[Competency]] = relationship(
        back_populates="curriculum_version", cascade="all, delete-orphan", passive_deletes=True
    )
    requirements: Mapped[list[RequirementRule]] = relationship(
        back_populates="curriculum_version", cascade="all, delete-orphan", passive_deletes=True
    )


class TrainingYear(Base, IdMixin, TimestampMixin, SyncMixin):
    """One year (or stage) of a programme."""

    __tablename__ = "training_years"
    __table_args__ = (
        UniqueConstraint("curriculum_version_id", "sequence", name="uq_training_year_seq"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    curriculum_version_id: Mapped[str] = mapped_column(
        ForeignKey("curriculum_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    level: Mapped[str] = mapped_column(String(32), default=TrainingLevel.REGISTRAR, nullable=False)
    duration_months: Mapped[int] = mapped_column(default=12, nullable=False)
    objectives: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    #: Expectations narrative for duties, professionalism, leadership, teaching, research.
    expectations: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    curriculum_version: Mapped[CurriculumVersion] = relationship(back_populates="training_years")
    rotations: Mapped[list[RotationTemplate]] = relationship(
        back_populates="training_year",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RotationTemplate.sequence",
    )


class RotationTemplate(Base, IdMixin, TimestampMixin, SyncMixin):
    """A planned posting within a training year."""

    __tablename__ = "rotation_templates"
    __table_args__ = (Index("ix_rotation_templates_year_seq", "training_year_id", "sequence"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    training_year_id: Mapped[str] = mapped_column(
        ForeignKey("training_years.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Host department/unit. NULL means "any unit that offers this posting".
    org_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="SET NULL"), default=None, index=True
    )
    specialty_id: Mapped[str | None] = mapped_column(
        ForeignKey("specialties.id", ondelete="SET NULL"), default=None
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str | None] = mapped_column(String(64), default=None)
    sequence: Mapped[int] = mapped_column(default=1, nullable=False)
    duration_weeks: Mapped[int] = mapped_column(default=12, nullable=False)
    is_elective: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_external: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_trainees: Mapped[int | None] = mapped_column(default=None)
    objectives: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    #: Assessment instrument codes required to close this rotation.
    required_assessments: Mapped[list[str]] = mapped_column(default=list, nullable=False)

    training_year: Mapped[TrainingYear] = relationship(back_populates="rotations")
    requirements: Mapped[list[RequirementRule]] = relationship(
        back_populates="rotation_template", passive_deletes=True
    )


class Competency(Base, IdMixin, TimestampMixin, SyncMixin):
    """A competency or Entrustable Professional Activity within a curriculum."""

    __tablename__ = "competencies"
    __table_args__ = (
        UniqueConstraint("curriculum_version_id", "code", name="uq_competency_curriculum_code"),
        Index("ix_competencies_domain", "curriculum_version_id", "domain"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    curriculum_version_id: Mapped[str] = mapped_column(
        ForeignKey("curriculum_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("competencies.id", ondelete="SET NULL"), default=None
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    domain: Mapped[str] = mapped_column(String(32), default=CompetencyDomain.PATIENT_CARE, nullable=False)
    is_epa: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Target entrustment level by the end of each training year:
    #: {"1": "2_direct_supervision", "2": "3_indirect_supervision", ...}
    target_by_year: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    exit_target: Mapped[str] = mapped_column(
        String(32), default=EntrustmentLevel.INDEPENDENT, nullable=False
    )
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    assessment_methods: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)

    curriculum_version: Mapped[CurriculumVersion] = relationship(back_populates="competencies")
    parent: Mapped[Competency | None] = relationship(remote_side="Competency.id")


class RequirementRule(Base, IdMixin, TimestampMixin, SyncMixin):
    """A single, machine-evaluable training requirement.

    This is the mechanism that makes institutional policy configurable rather than
    hard-coded. A rule says: *within this scope, this measurable quantity must satisfy
    this operator against this target*. The evaluator in
    ``app.services.requirements`` knows how to measure every ``RequirementKind``; adding
    or changing a policy is a data edit, never a deployment.

    Examples
    --------
    "40 major operations performed under supervision in year 3"::

        kind=PROCEDURE_ROLE_COUNT, operator=GTE, target_value=40,
        scope=TRAINING_YEAR, parameters={"role": "performed_supervised",
                                         "category": "major", "training_year": 3}

    "80% attendance at Grand Rounds across the programme"::

        kind=ACADEMIC_ATTENDANCE_PCT, operator=GTE, target_value=80,
        scope=PROGRAMME, parameters={"activity_kinds": ["grand_round"]}
    """

    __tablename__ = "requirement_rules"
    __table_args__ = (
        Index("ix_requirement_rules_scope", "curriculum_version_id", "scope"),
        Index("ix_requirement_rules_rotation", "rotation_template_id"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    curriculum_version_id: Mapped[str] = mapped_column(
        ForeignKey("curriculum_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    training_year_id: Mapped[str | None] = mapped_column(
        ForeignKey("training_years.id", ondelete="CASCADE"), default=None, index=True
    )
    rotation_template_id: Mapped[str | None] = mapped_column(
        ForeignKey("rotation_templates.id", ondelete="CASCADE"), default=None
    )
    competency_id: Mapped[str | None] = mapped_column(
        ForeignKey("competencies.id", ondelete="CASCADE"), default=None
    )

    code: Mapped[str | None] = mapped_column(String(64), default=None)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    operator: Mapped[str] = mapped_column(String(8), default=RequirementOperator.GTE, nullable=False)
    target_value: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Free-form measurement parameters interpreted by the evaluator for this ``kind``.
    parameters: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    scope: Mapped[str] = mapped_column(String(24), default=RequirementScope.PROGRAMME, nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), default=RequirementSeverity.MANDATORY, nullable=False
    )
    #: Contribution of this rule to its score domain (relative weight).
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    score_domain: Mapped[str | None] = mapped_column(String(32), default=None)
    guidance: Mapped[str | None] = mapped_column(Text, default=None)
    #: Reference to the college regulation this rule implements, for audit purposes.
    source_reference: Mapped[str | None] = mapped_column(String(255), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    curriculum_version: Mapped[CurriculumVersion] = relationship(back_populates="requirements")
    rotation_template: Mapped[RotationTemplate | None] = relationship(back_populates="requirements")
    competency: Mapped[Competency | None] = relationship()

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.label} [{self.kind} {self.operator} {self.target_value}]"


class ProcedureCatalogueItem(Base, IdMixin, TimestampMixin, SyncMixin):
    """The list of procedures a department recognises for logbook entry.

    Institution-editable, so a new operation can be logged the day it is introduced.
    """

    __tablename__ = "procedure_catalogue"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_procedure_catalogue_code"),
        Index("ix_procedure_catalogue_specialty", "tenant_id", "specialty_id"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    specialty_id: Mapped[str | None] = mapped_column(
        ForeignKey("specialties.id", ondelete="SET NULL"), default=None
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="general", nullable=False)
    #: "major" | "minor" | "intermediate" — drives requirement counting.
    grade: Mapped[str] = mapped_column(String(24), default="minor", nullable=False)
    #: Optional external coding, e.g. ICD-9-CM / OPCS / SNOMED.
    external_codes: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    default_complexity: Mapped[str | None] = mapped_column(String(24), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    specialty: Mapped[Specialty | None] = relationship()
