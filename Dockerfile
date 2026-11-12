# syntax=docker/dockerfile:1

# Base images are pinned by tag only; Dependabot (docker ecosystem) keeps the tag
# current. Pinning by digest is a follow-up (digests cannot be resolved offline).

# ---- builder: resolve and install locked, non-dev dependencies into /app/.venv
FROM python:3.15-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependency layer first (cached until the lock file changes). README.md is
# required here because the build backend (hatchling) reads it as package metadata.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-editable --no-install-project

# Project layer: install s1gw itself (non-editable) into the same venv.
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime: only the venv, no uv, no build tooling, no source tree
FROM python:3.15-slim AS runtime

LABEL org.opencontainers.image.source="https://github.com/zanglang/systemone-gemini-gateway"

RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

# Owned by root and world-readable: the app user cannot modify its own code.
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
EXPOSE 8080 9464

CMD ["uvicorn", "s1gw.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
