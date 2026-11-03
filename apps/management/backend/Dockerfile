FROM ghcr.io/astral-sh/uv:0.11.7 AS uv
FROM python:3.12-slim-bookworm AS runtime
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY scripts/management/stage_workspace.py /tmp/stage_workspace.py
RUN --mount=type=bind,source=.,target=/workspace-context,readonly \
    python /tmp/stage_workspace.py /workspace-context /app \
    && uv sync --locked --package platform-management --no-dev  && useradd --uid 10001 --create-home management  && mkdir -p /artifacts /app/artifacts/management  && chown -R 10001:10001 /artifacts /app/artifacts
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
