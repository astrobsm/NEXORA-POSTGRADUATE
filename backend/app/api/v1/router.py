"""API v1 router assembly."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    academic,
    accreditation,
    analytics,
    assessments,
    auth,
    cbt,
    curriculum,
    learning,
    logbook,
    meta,
    research,
    sync,
    tenancy,
    training,
    users,
)

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["Authentication"])
api_router.include_router(meta.router, prefix="/meta", tags=["Reference & notifications"])
api_router.include_router(tenancy.router, prefix="/tenancy", tags=["Institutions & hierarchy"])
api_router.include_router(users.router, prefix="/users", tags=["Users & roles"])
api_router.include_router(curriculum.router, prefix="/curriculum", tags=["Curriculum builder"])
api_router.include_router(training.router, prefix="/training", tags=["Training & rotations"])
api_router.include_router(logbook.router, prefix="/logbook", tags=["Digital logbook"])
api_router.include_router(assessments.router, prefix="/assessments", tags=["Assessment"])
api_router.include_router(academic.router, prefix="/academic", tags=["Academic activities & CME"])
api_router.include_router(research.router, prefix="/research", tags=["Research & dissertation"])
api_router.include_router(analytics.router, prefix="/analytics", tags=["Analytics & promotion"])
api_router.include_router(accreditation.router, prefix="/accreditation", tags=["Accreditation"])
api_router.include_router(cbt.router, prefix="/cbt", tags=["Computer-based testing"])
api_router.include_router(
    learning.router, prefix="/learning", tags=["Reading, readiness & remediation"]
)
api_router.include_router(sync.router, prefix="/sync", tags=["Offline synchronisation"])
