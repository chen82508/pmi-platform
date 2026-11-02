FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir "uv>=0.4.0"

WORKDIR /app

COPY pyproject.toml ./
COPY pmi_api ./pmi_api

# pmi-core is bind-mounted at /app/pmi_core via docker-compose volumes.
# Install runtime deps needed by both packages.
RUN uv pip install --system --no-cache \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.30.0" \
    "structlog>=24.1.0" \
    "pydantic>=2.7.0" \
    "pydantic-settings>=2.3.0" \
    "sqlalchemy[asyncio]>=2.0.30" \
    "asyncpg>=0.29.0" \
    "pgvector>=0.3.0" \
    "pyyaml>=6.0.1" \
    "click>=8.1.7" \
    "httpx>=0.27.0" \
    "tenacity>=8.4.0" \
    "alembic>=1.13.2" \
    "numpy>=1.26" \
    "sentry-sdk>=2.0.0" \
    "mcp>=1.9,<2" \
    "redis>=5.0.0"

EXPOSE 8000
ENTRYPOINT ["python", "-m", "pmi_api.main"]
