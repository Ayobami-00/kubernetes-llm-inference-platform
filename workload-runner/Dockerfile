FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv
FROM python:3.12.16-bookworm@sha256:a5bab0ff300f91231c2e68bf73a8a8d60762ac39ce1c50eaf6803e4c38a9062b AS build
COPY --from=uv /uv /bin/uv
WORKDIR /app
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --package workload-runner --all-extras --no-dev --no-editable
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
COPY --from=build /app /app
RUN mkdir -p /workspace /home/runner \
    && chown -R 10001:10001 /workspace /home/runner
ENV PATH="/app/.venv/bin:$PATH" HOME=/home/runner PYTHONUNBUFFERED=1 TOKENIZERS_PARALLELISM=false
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["workload-runner", "--repo", "/app"]
