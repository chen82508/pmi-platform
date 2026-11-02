FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir "uv>=0.4.0"

WORKDIR /app

COPY pyproject.toml ./
COPY pmi_mcp ./pmi_mcp

# Runtime deps are hand-listed (same convention as the other service images —
# a dep added only to pyproject never reaches the image).
RUN uv pip install --system --no-cache \
    "mcp>=1.9,<2" \
    "httpx>=0.27.0"

EXPOSE 8100
ENTRYPOINT ["python", "-m", "pmi_mcp.server"]
