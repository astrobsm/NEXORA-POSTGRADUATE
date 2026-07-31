"""Engine and session factory."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings


def engine_kwargs() -> dict[str, Any]:
    """The engine strategy for the current runtime, as plain keyword arguments.

    Separated from :func:`_build_engine` purely so it can be asserted on. Whether
    the connection strategy is right is not observable from a built ``Engine`` —
    ``connect_args`` are merged at connect time and never surface — so a test that
    inspects the engine proves nothing. This function is the seam.

    Three distinct cases, and getting them wrong is the usual cause of a
    deployment that works locally and falls over in production:

    **SQLite** — a file. Threads share it, so the same-thread check is disabled.

    **Long-running PostgreSQL** (Docker, Kubernetes) — a real connection pool.
    Connections are reused across many requests, which is what a pool is for.

    **Serverless PostgreSQL** (Vercel, Lambda) — *no* pool. A function is frozen
    between invocations, so a pooled connection it holds is probably dead when it
    thaws; and with hundreds of concurrent functions each holding a pool, the
    database runs out of connections long before the application runs out of work.
    Pooling belongs in front of the database (Supabase's PgBouncer), not inside
    each ephemeral function.
    """
    kwargs: dict[str, Any] = {"echo": settings.db_echo, "future": True}

    if settings.is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["pool_pre_ping"] = True
        return kwargs

    connect_args: dict[str, Any] = {}

    if settings.uses_pgbouncer:
        # PgBouncer in transaction mode hands a different backend connection to
        # each transaction, so a prepared statement created on one is not there
        # on the next. psycopg prepares automatically after five executions of a
        # query, which is exactly the kind of bug that only appears under load.
        connect_args["prepare_threshold"] = None

    if settings.is_serverless:
        kwargs["poolclass"] = NullPool
        # pool_pre_ping is pointless with NullPool — every connection is new.
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
        kwargs["pool_pre_ping"] = True
        # Managed Postgres and load balancers drop idle connections; recycling
        # below that window avoids handing out a corpse.
        kwargs["pool_recycle"] = 1800

    if connect_args:
        kwargs["connect_args"] = connect_args

    return kwargs


def build_engine() -> Engine:
    return create_engine(settings.database_url, **engine_kwargs())


engine = build_engine()


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """SQLite needs foreign keys switched on explicitly, and WAL for concurrent reads."""
    if not settings.is_sqlite:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Transactional scope for scripts, jobs and the seeder."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
