"""Shared Pydantic contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True, ser_json_timedelta="float")


class Page(ApiModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    page_size: int = 50

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


class PageParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=500)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class Message(BaseModel):
    detail: str


class IdRef(ApiModel):
    id: str
    name: str | None = None


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None
    field_errors: dict[str, list[str]] | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    database: str
    time: datetime


# --------------------------------------------------------------------------
# Cross-cutting result shapes produced by the engines
# --------------------------------------------------------------------------
class RequirementResultOut(BaseModel):
    rule_id: str
    code: str | None = None
    label: str
    kind: str
    scope: str
    severity: str
    operator: str
    target: float
    measured: float
    met: bool
    shortfall: float
    progress_percent: float
    weight: float = 1.0
    score_domain: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    guidance: str | None = None


class GapOut(BaseModel):
    rule_id: str | None = None
    label: str
    severity: str
    scope: str | None = None
    domain: str | None = None
    measured: float
    target: float
    shortfall: float
    progress_percent: float
    guidance: str | None = None


class DomainScoreOut(BaseModel):
    domain: str
    score: float
    rag: str
    contributing_rules: int = 0
    signals: dict[str, Any] = Field(default_factory=dict)


class ScoreReportOut(BaseModel):
    enrolment_id: str
    computed_at: datetime
    training_year: int
    overall_score: float
    overall_rag: str
    promotion_readiness_score: float
    domains: dict[str, DomainScoreOut]
    gaps: list[GapOut]
    metrics: dict[str, Any]
    weights_used: dict[str, float]
    #: Domains this curriculum does not assess. They are shown as "not assessed" rather
    #: than zero, and are excluded from the overall score.
    unassessed_domains: list[str] = Field(default_factory=list)
    effective_weight_base: float = 1.0
    requirement_results: list[RequirementResultOut] = Field(default_factory=list)


class PromotionAssessmentOut(BaseModel):
    enrolment_id: str
    from_level: str
    to_level: str
    from_year: int
    to_year: int
    outcome: str
    readiness_percent: float
    rationale: str
    time_served_months: int
    minimum_months_required: int
    blocking: list[RequirementResultOut] = Field(default_factory=list)
    advisories: list[RequirementResultOut] = Field(default_factory=list)
    checks: dict[str, Any] = Field(default_factory=dict)


class CandidateOut(BaseModel):
    user_id: str
    name: str
    score: float
    components: dict[str, float]
    reasons: list[str]
    current_load: int
    capacity: int
    eligible: bool
    exclusion_reason: str | None = None


class DateRange(BaseModel):
    start: date
    end: date
