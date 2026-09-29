# syntax=docker/dockerfile:1

# --- Build: resolve dependencies and install the app into a virtualenv -------
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.20 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first: this layer is reused as long as the lockfile doesn't change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- Runtime: Python + the virtualenv, nothing else ---------------------------
FROM python:3.13-slim

# UID 1000 matches the first user of most Linux hosts, which eases bind mounts.
RUN useradd --system --uid 1000 --no-create-home otter \
    && mkdir /data && chown otter:otter /data
COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    OTTER_DATA_DIR=/data

USER otter
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

CMD ["uvicorn", "otter.main:app", "--host", "0.0.0.0", "--port", "8000"]
