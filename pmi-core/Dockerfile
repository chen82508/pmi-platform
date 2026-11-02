FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

RUN pip install --no-cache-dir "uv>=0.4.0"

WORKDIR /app

# Step 1: install deps only (cache-friendly). Use --no-install-package on the local
# package so hatchling doesn't try to build a wheel before sources are copied in.
COPY pyproject.toml README.md ./
RUN uv pip install --system --no-cache \
    "sqlalchemy[asyncio]>=2.0.30" \
    "asyncpg>=0.29.0" \
    "alembic>=1.13.2" \
    "pgvector>=0.3.0" \
    "pydantic>=2.7.0" \
    "pydantic-settings>=2.3.0" \
    "pyyaml>=6.0.1" \
    "structlog>=24.1.0" \
    "click>=8.1.7" \
    "httpx>=0.27.0" \
    "tenacity>=8.4.0" \
    "rich>=13.7.0" \
    "google-auth>=2.29.0" \
    "google-auth-oauthlib>=1.2.0" \
    "numpy>=1.26" \
    "mlflow-skinny>=2.18.0,<3" \
    "openai>=1.40.0"

# Step 2: copy source then install the package itself (now hatchling can find
# pmi_core/). PYTHONPATH=/app above also lets the bind-mounted dev source
# transparently shadow site-packages for hot iteration.
COPY pmi_core ./pmi_core
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY tests ./tests
RUN uv pip install --system --no-cache --no-deps .

ENTRYPOINT ["pmi-core"]
CMD ["--help"]
