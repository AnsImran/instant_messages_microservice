# syntax=docker/dockerfile:1.7
#
# Production image for the Instant Messages FastAPI microservice.
# Uses `uv` to install from the committed lockfile so builds are reproducible.

FROM python:3.12-slim AS base

# ---------------------------------------------------------------------------
# Base runtime: small, unbuffered Python. `uv` is installed from its official
# image so we never have to pip-install it (faster, deterministic).
# ---------------------------------------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app

# ---------------------------------------------------------------------------
# Dependency layer — cached as long as pyproject.toml + uv.lock don't change.
# ---------------------------------------------------------------------------
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# ---------------------------------------------------------------------------
# Source layer — only this rebuilds when src/ or main.py changes.
# ---------------------------------------------------------------------------
COPY main.py ./
COPY src/    ./src/
COPY config/ ./config/

# Final install so the project itself is registered in the venv.
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Runtime.
# ---------------------------------------------------------------------------
EXPOSE 8000

# Uvicorn is bundled inside our dependency set via `uvicorn[standard]`.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
