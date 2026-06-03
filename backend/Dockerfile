# =============================================================================
# Backend Dockerfile — FastAPI + uv
# =============================================================================
# Multi-stage build for smaller final image.

FROM python:3.11-slim AS base

# Install uv (fast Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (cache-friendly layer)
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

# Copy application code
COPY . .

# Expose port
EXPOSE 8000

# Run with uvicorn
RUN uv sync --frozen --no-dev && echo "uv sync done"

CMD [".venv/bin/python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
