FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv
FROM python:3.12.13-bookworm@sha256:3cd9086bdb30f7c9bc08a3fa621d9842e0d3f6f9291aeb4677e0547817c10b12 AS build
COPY --from=uv /uv /bin/uv
WORKDIR /app
COPY . .
RUN uv sync --locked --package workload-runner --all-extras --no-dev --no-editable
FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
COPY --from=build /app /app
RUN mkdir -p /workspace /home/runner \
    && chown -R 10001:10001 /workspace /home/runner
ENV PATH="/app/.venv/bin:$PATH" HOME=/home/runner PYTHONUNBUFFERED=1 TOKENIZERS_PARALLELISM=false
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["workload-runner", "--repo", "/app"]
