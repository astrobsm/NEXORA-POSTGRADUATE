"""Residency Training Console — FastAPI application entrypoint."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.router import api_router
from app.core.config import settings
from app.db.base import Base, utcnow
from app.db.session import engine
from app.schemas.common import HealthResponse

VERSION = "1.0.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
logger = logging.getLogger("rtc")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Importing the model package registers every mapper before the first request.
    import app.models  # noqa: F401

    if settings.is_sqlite and settings.env in {"local", "development"}:
        # Convenience for zero-infrastructure local runs. Postgres deployments are
        # migrated with Alembic; `create_all` is never used against them.
        Base.metadata.create_all(bind=engine)
        logger.info("SQLite schema ensured at %s", settings.database_url)

    logger.info("%s v%s starting in %s mode | %s",
                settings.app_name, VERSION, settings.env, settings.vendor_statement)
    if not settings.is_sqlite:
        logger.info(
            "Database: PostgreSQL | serverless=%s | pgbouncer=%s",
            settings.is_serverless, settings.uses_pgbouncer,
        )
    yield
    logger.info("%s shutting down", settings.app_short_name)


app = FastAPI(
    title=settings.app_name,
    version=VERSION,
    description=(
        "Enterprise, offline-first, competency-based medical education platform for "
        "postgraduate residency, internship, fellowship and CME training.\n\n"
        "**Multi-tenant** across National → College → Hospital → Faculty → Department → "
        "Unit → Subspecialty → Programme.\n\n"
        "**Policy is data**: training requirements, assessment instruments, notification "
        "rules and accreditation standards are all configured, not coded."
        "\n\n---\n\n"
        f"*{settings.vendor_statement}*"
    ),
    openapi_url=f"{settings.api_v1_prefix}/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
    contact={"name": settings.vendor_name, "url": settings.vendor_url},
    license_info={"name": f"Proprietary — © {settings.vendor_name}"},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id", "X-Response-Time-Ms"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request id and timing to every response, for tracing and audit."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
    if elapsed_ms > 2000:
        logger.warning("Slow request %s %s took %.0fms", request.method, request.url.path, elapsed_ms)
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(self), camera=(), microphone=()"
    )
    if settings.is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


# --------------------------------------------------------------------------
# Error handling — messages a clinician can act on, never a stack trace.
# --------------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    field_errors: dict[str, list[str]] = {}
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"][1:]) or "body"
        field_errors.setdefault(location, []).append(error["msg"])
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Some fields need attention before this can be saved.",
            "code": "validation_error",
            "field_errors": field_errors,
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": f"http_{exc.status_code}"},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception("Unhandled error on %s %s [%s]", request.method, request.url.path, request_id)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Something went wrong at our end. The error has been logged; quote "
                      f"reference {request_id} if you contact support.",
            "code": "internal_error",
            "request_id": request_id,
        },
    )


# --------------------------------------------------------------------------
app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/health", response_model=HealthResponse, tags=["Service"], summary="Health check")
def health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:  # pragma: no cover - surfaced to orchestrators
        logger.error("Health check database failure: %s", exc)
        database = "unavailable"

    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        version=VERSION,
        environment=settings.env,
        database=database,
        time=utcnow(),
    )


@app.get("/", tags=["Service"], summary="Service banner")
def root():
    return {
        "name": settings.app_name,
        "short_name": settings.app_short_name,
        "version": VERSION,
        "vendor": {
            "name": settings.vendor_name,
            "url": settings.vendor_url,
            "statement": settings.vendor_statement,
        },
        "docs": "/docs",
        "openapi": f"{settings.api_v1_prefix}/openapi.json",
        "health": "/health",
    }
