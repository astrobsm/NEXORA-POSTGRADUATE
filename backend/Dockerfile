# syntax=docker/dockerfile:1.7
# --------------------------------------------------------------------------
# Residency Training Console — API
# --------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --------------------------------------------------------------------------
# Dependencies are installed in their own layer so application changes do not
# invalidate the (slow) wheel build.
# --------------------------------------------------------------------------
FROM base AS deps

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# --------------------------------------------------------------------------
FROM base AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    # Runs unprivileged: the API needs no root capability, and a container
    # holding training records should not have one.
    && useradd --create-home --uid 10001 rtc

COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY --chown=rtc:rtc alembic.ini pyproject.toml ./
COPY --chown=rtc:rtc alembic ./alembic
COPY --chown=rtc:rtc app ./app
COPY --chown=rtc:rtc docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh && chown -R rtc:rtc /app

USER rtc
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
