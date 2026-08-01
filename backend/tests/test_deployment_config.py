"""Deployment configuration.

The database connection strategy has to invert between a long-running container
and a serverless function, and the failure mode of getting it wrong is nasty: it
works in testing, then exhausts the database's connection limit or serves
prepared statements that do not exist once real traffic arrives.

None of that is observable from a built ``Engine`` — ``connect_args`` are merged
at connect time and never surface again — so these tests assert on
``engine_kwargs()``, which is the seam that exists for exactly this reason.
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.db import session as session_module

SUPABASE_TRANSACTION = (
    "postgresql+psycopg://postgres.abcdefgh:pw"
    "@aws-0-eu-west-2.pooler.supabase.com:6543/postgres"
)
SUPABASE_SESSION = (
    "postgresql+psycopg://postgres.abcdefgh:pw"
    "@aws-0-eu-west-2.pooler.supabase.com:5432/postgres"
)
SELF_HOSTED = "postgresql+psycopg://rtc:pw@postgres:5432/rtc"


@pytest.fixture
def configure(monkeypatch):
    """Swap in a Settings built for a specific runtime."""

    def _configure(**overrides) -> Settings:
        settings = Settings(secret_key="test", **overrides)
        monkeypatch.setattr(session_module, "settings", settings)
        return settings

    return _configure


# ==========================================================================
class TestServerlessStrategy:
    def test_vercel_with_supabase_does_not_pool_in_process(self, configure):
        """Hundreds of concurrent functions each holding a pool would exhaust the
        database's connection limit long before the app ran out of work."""
        configure(database_url=SUPABASE_TRANSACTION, db_serverless=True)
        kwargs = session_module.engine_kwargs()

        assert kwargs["poolclass"] is NullPool
        assert "pool_size" not in kwargs
        assert "max_overflow" not in kwargs

    def test_pgbouncer_transaction_mode_disables_prepared_statements(self, configure):
        """psycopg prepares a statement automatically after five executions. In
        transaction mode each transaction may land on a different backend, where
        that prepared statement does not exist — a bug that only appears under
        load, which is the worst kind."""
        configure(database_url=SUPABASE_TRANSACTION, db_serverless=True)
        kwargs = session_module.engine_kwargs()

        assert kwargs["connect_args"]["prepare_threshold"] is None

    def test_pre_ping_is_not_wasted_on_a_null_pool(self, configure):
        """Every NullPool connection is brand new; pinging it is a pointless
        round trip on a latency-sensitive cold start."""
        configure(database_url=SUPABASE_TRANSACTION, db_serverless=True)
        assert "pool_pre_ping" not in session_module.engine_kwargs()

    def test_serverless_is_auto_detected_from_the_platform(self, configure, monkeypatch):
        monkeypatch.setenv("VERCEL", "1")
        settings = configure(database_url=SUPABASE_TRANSACTION)
        assert settings.is_serverless is True

    def test_detection_can_be_overridden(self, configure, monkeypatch):
        """A container running on a host that happens to set VERCEL must still be
        able to pool."""
        monkeypatch.setenv("VERCEL", "1")
        settings = configure(database_url=SELF_HOSTED, db_serverless=False)
        assert settings.is_serverless is False
        assert "pool_size" in session_module.engine_kwargs()


class TestLongRunningStrategy:
    def test_a_container_pools(self, configure):
        configure(database_url=SELF_HOSTED)
        kwargs = session_module.engine_kwargs()

        assert "poolclass" not in kwargs
        assert kwargs["pool_size"] == 10
        assert kwargs["max_overflow"] == 20
        assert kwargs["pool_pre_ping"] is True

    def test_connections_are_recycled_before_the_infrastructure_drops_them(self, configure):
        """Managed Postgres and load balancers close idle connections. Recycling
        below that window is the difference between a clean reconnect and an
        intermittent 'server closed the connection unexpectedly'."""
        configure(database_url=SELF_HOSTED)
        assert session_module.engine_kwargs()["pool_recycle"] == 1800

    def test_a_direct_connection_keeps_prepared_statements(self, configure):
        """Prepared statements are a real performance win; they are only disabled
        because PgBouncer cannot serve them."""
        configure(database_url=SELF_HOSTED)
        assert "connect_args" not in session_module.engine_kwargs()


class TestSqliteStrategy:
    def test_sqlite_shares_across_threads(self, configure):
        configure(database_url="sqlite:///./local.db")
        kwargs = session_module.engine_kwargs()

        assert kwargs["connect_args"]["check_same_thread"] is False
        assert "poolclass" not in kwargs


class TestPoolerDetection:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (SUPABASE_TRANSACTION, True),
            (SUPABASE_SESSION, False),
            (SELF_HOSTED, False),
            ("postgresql+psycopg://u:p@host:5432/db?pgbouncer=true", True),
        ],
    )
    def test_transaction_pooler_is_recognised(self, url, expected):
        assert Settings(secret_key="t", database_url=url).uses_pgbouncer is expected


class TestVendorIdentity:
    def test_attribution_is_configured(self):
        settings = Settings(secret_key="t")
        assert settings.vendor_name == "NEXORA Innovations"
        assert "NEXORA Innovations" in settings.vendor_statement

    def test_attribution_reaches_the_api_surface(self, client):
        """The vendor statement must survive into what a consumer actually sees."""
        response = client.get("/")
        assert response.status_code == 200
        assert response.json()["vendor"]["name"] == "NEXORA Innovations"

        schema = client.get("/api/v1/openapi.json").json()
        assert schema["info"]["contact"]["name"] == "NEXORA Innovations"
        assert "NEXORA Innovations" in schema["info"]["description"]
        assert "NEXORA Innovations" in schema["info"]["license"]["name"]
