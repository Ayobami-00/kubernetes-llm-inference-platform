FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv
FROM amazon/aws-cli:2.34.53@sha256:ef742142dec4542dda7c92a19d3162d6b63d068c3c8c31f6e3e08d5b015d7cbe AS awscli
FROM python:3.12.16-slim-trixie@sha256:1148dbb014615300fa7e713b2f94f550c0d1c69780c0805f84f093ceee34dd0b AS sshbuild
RUN apt-get update && apt-get install --no-install-recommends -y build-essential libssl-dev zlib1g-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/infrastructure/host_executor_openssh.py /tmp/host_executor_openssh.py
RUN python /tmp/host_executor_openssh.py

FROM python:3.12.16-slim-trixie@sha256:1148dbb014615300fa7e713b2f94f550c0d1c69780c0805f84f093ceee34dd0b
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=awscli /usr/local/aws-cli /usr/local/aws-cli
COPY --from=sshbuild /opt/klip/openssh /opt/klip/openssh
RUN apt-get update && apt-get install --no-install-recommends -y ca-certificates binutils libssl3t64 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/local/aws-cli/v2/current/bin/aws /usr/local/bin/aws \
    && for binary in ssh scp sftp ssh-keygen; do ln -s "/opt/klip/openssh/bin/$binary" "/usr/local/bin/$binary"; done
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY scripts/management/stage_workspace.py /tmp/stage_workspace.py
RUN --mount=type=bind,source=.,target=/workspace-context,readonly \
    python /tmp/stage_workspace.py /workspace-context /app \
    && uv sync --locked --package kubernetes-llm-inference-platform --package platform-management --group infrastructure --no-dev
COPY scripts/infrastructure scripts/infrastructure
COPY infra/ansible infra/ansible
COPY infra/terraform/project.json infra/terraform/platform.json infra/terraform/
COPY deploy/artifacts.lock.json deploy/gpu-host.lock.json deploy/gpu-operator.lock.json deploy/
COPY deploy/host-executor-aws-config deploy/
ENV PATH="/app/.venv/bin:/app/artifacts/tools/bin:$PATH" \
    PYTHONPATH=/app/scripts/infrastructure PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    KLIP_STATE_HOME=/executor/state KLIP_SSH_SOCKET_HOME=/tmp/klip-ssh \
    HOME=/tmp LANG=C.UTF-8 LC_ALL=C.UTF-8 AWS_EC2_METADATA_DISABLED=true
RUN python scripts/infrastructure/host_executor_image.py \
    && aws --version && session-manager-plugin --version && ansible-playbook --version && ssh -V \
    && rm /usr/local/bin/uv \
    && useradd --uid 20002 --no-create-home executor \
    && mkdir -p /executor/state /app/artifacts/ansible \
    && chown 20002:20002 /executor/state /app/artifacts/ansible
USER 20002:20002
CMD ["python", "-m", "platform_management.host_executor.api"]
