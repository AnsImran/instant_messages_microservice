# syntax=docker/dockerfile:1.7
#
# Production image for the Instant Messages FastAPI microservice.
# Uses `uv` to install from the committed lockfile so builds are reproducible.
# CMD wraps `uvicorn` with `opentelemetry-instrument`; traces ship when
# OTEL_* env vars are set by docker-compose.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer — cached unless pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# OTel auto-instrumentors (fastapi, httpx, logging, asgi, stdlib).
RUN opentelemetry-bootstrap -a install

# Source.
COPY main.py ./
COPY src/    ./src/
COPY config/ ./config/

# Final install so the project itself is registered in the venv.
RUN uv sync --frozen --no-dev

EXPOSE 8000

# opentelemetry-instrument wraps uvicorn. Inert when OTEL env vars absent.
CMD ["opentelemetry-instrument", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
