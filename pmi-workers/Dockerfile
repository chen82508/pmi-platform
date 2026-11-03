FROM python:3.12-slim

# Supercronic — same arch-aware install as the legacy job executor so
# the resulting Render / Fly cron container is interchangeable.
ARG SUPERCRONIC_VERSION=0.2.45
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && ARCH=$(uname -m) \
    && case "$ARCH" in \
        x86_64)  SC_ARCH="amd64" ;; \
        aarch64) SC_ARCH="arm64" ;; \
        *) echo "Unsupported arch: $ARCH" && exit 1 ;; \
    esac \
    && curl -fsSLo /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-${SC_ARCH}" \
    && chmod +x /usr/local/bin/supercronic \
    && apt-get purge -y curl \
    && apt-get autoremove -y && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # pmi_core is bind-mounted at /app/pmi_core (see docker-compose volumes).
    # Console scripts like `run-job` live in /usr/local/bin so they don't get
    # /app on sys.path automatically — explicitly put it there so supercronic
    # child processes can import pmi_core too. Matches the pmi-ingest pattern.
    PYTHONPATH=/app

RUN pip install --no-cache-dir "uv>=0.4.0"

WORKDIR /app

# pmi-core is bind-mounted at /app/pmi_core via docker-compose volumes,
# matching how pmi-api / pmi-ingest do it. Only install workers' own deps
# at build time.
# README.md is COPY'd because pyproject.toml's `readme = "README.md"` field
# makes hatchling fail the wheel build if it's missing.
COPY pyproject.toml README.md ./
COPY pmi_workers ./pmi_workers
COPY cron ./cron

RUN uv pip install --system --no-cache \
    "click>=8.1.7" \
    "structlog>=24.1.0" \
    "sqlalchemy[asyncio]>=2.0.30" \
    "asyncpg>=0.29.0" \
    "pgvector>=0.3.0" \
    "pydantic>=2.7.0" \
    "pydantic-settings>=2.3.0" \
    "pyyaml>=6.0.1" \
    "httpx>=0.27.0" \
    "cryptography>=42.0" \
    "tenacity>=8.4.0" \
    "alembic>=1.13.2" \
    "numpy>=1.26" \
    "mlflow-skinny>=2.22,<3" \
    "openai>=1.40.0" \
    "sentry-sdk>=2.0.0" \
    "google-auth>=2.29.0" \
    "google-auth-oauthlib>=1.2.0"

# Install the project (no deps — picked them up above) so `run-job`
# console script lands on PATH.
RUN uv pip install --system --no-cache --no-deps .

# Default: run supercronic against the bundled crontab. Override with
# `docker run pmi-workers run-job <name>` or `pmi-workers <subcommand>` for
# one-shot invocations.
ENTRYPOINT []
CMD ["supercronic", "-passthrough-logs", "/app/cron/crontab"]
