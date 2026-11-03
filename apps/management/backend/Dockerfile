FROM ghcr.io/astral-sh/uv:0.11.7 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY packages/platform-contracts packages/platform-contracts
COPY apps/management/backend apps/management/backend
RUN uv sync --locked --all-packages --no-dev
COPY configs configs
COPY datasets/manifests datasets/manifests
COPY contracts/fixtures contracts/fixtures
ENV PATH="/app/.venv/bin:$PATH" MANAGEMENT_REPOSITORY_ROOT=/app PYTHONUNBUFFERED=1
CMD ["uvicorn", "platform_management.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
