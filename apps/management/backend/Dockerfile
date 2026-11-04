FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS runtime
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY scripts/management/stage_workspace.py /tmp/stage_workspace.py
RUN --mount=type=bind,source=.,target=/workspace-context,readonly \
    python /tmp/stage_workspace.py /workspace-context /app \
    && uv sync --locked --package platform-management --no-dev \
    && .venv/bin/python -m platform_management.jobs.provider_prerequisites --help >/dev/null \
    && useradd --uid 10001 --create-home management \
    && mkdir -p /artifacts /app/artifacts/management \
    && chown -R 10001:10001 /artifacts /app/artifacts
COPY configs configs
COPY datasets/manifests datasets/manifests
COPY contracts/fixtures contracts/fixtures
ENV PATH="/app/.venv/bin:$PATH" MANAGEMENT_REPOSITORY_ROOT=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["uvicorn", "platform_management.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

# Local Compose bind-mounted secret files retain host ownership.
FROM runtime AS development
USER root

FROM runtime AS production
USER 10001:10001
