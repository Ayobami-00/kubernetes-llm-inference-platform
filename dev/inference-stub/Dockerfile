FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv
FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
COPY --from=uv /uv /bin/uv
WORKDIR /app
COPY . .
RUN uv sync --locked --package inference-stub --no-dev --no-editable
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8001
ENTRYPOINT ["inference-stub", "--host", "0.0.0.0", "--port", "8001"]
