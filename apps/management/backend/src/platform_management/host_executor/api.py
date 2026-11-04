"""Internal typed host-task API. Its only production listener requires mutual TLS."""

import json
import os
import ssl
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from platform_contracts import HostTask, HostTaskObservation

from platform_management.providers.base import TransientProviderError
from platform_management.services.common import AppError

MAX_BODY = 8192
TLS_HOME = Path("/run/secrets/host-executor-tls")


class BoundedBody:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > MAX_BODY:
                return await JSONResponse({"error": "Host task exceeds size limit"}, 413)(
                    scope, receive, send
                )
            chunks.append(message)
            if not message.get("more_body"):
                break

        async def replay():
            return chunks.pop(0) if chunks else await receive()

        await self.app(scope, replay, send)


def metadata(root):
    """Expose public bootstrap metadata; never transport credentials or private state."""
    from cluster_models import PeerRegistry

    from platform_management.host_executor.gpu_approval import configured

    inputs = json.loads((root / "cluster/executor-inputs.json").read_text())
    peers = PeerRegistry.model_validate_json((root / "cluster/peers.json").read_text())
    expected = {"cp-01", "cp-02", "cp-03", "platform-01", "platform-02", "platform-03"}
    if (
        inputs.get("schema_version") != "klip-host-executor-inputs.v1"
        or inputs.get("cluster_id") != "klip"
        or set(inputs.get("cpu_peers", {})) != expected
        or any(
            peers.model_dump(mode="json")["peers"].get(k) != inputs["cpu_peers"][k]
            for k in expected
        )
    ):
        raise ValueError("Executor protected cluster identity differs")
    public_key = (root / "cluster/lambda-ssh-key.pub").read_text().strip()
    if not public_key.startswith("ssh-ed25519 ") or len(public_key.split()) != 2:
        raise ValueError("Executor requires one dedicated public bootstrap key")
    return {
        "schema_version": "klip-host-executor-metadata.v1",
        "cluster_id": "klip",
        "kube_system_uid": inputs["kube_system_uid"],
        "registry_revision": peers.revision,
        "occupied_wireguard_ips": sorted(str(p.wireguard_ip) for p in peers.peers.values()),
        "ssh_source_cidrs": inputs["ssh_source_cidrs"],
        "bootstrap_public_key": public_key,
        **configured(root, inputs["kube_system_uid"]),
    }


def create_app(controller):
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.add_middleware(BoundedBody)

    @app.exception_handler(AppError)
    async def rejected(_, exc):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(_, exc):
        # Default FastAPI errors echo caller input. Retain no arbitrary payloads.
        return JSONResponse({"error": "Invalid typed host task"}, 422)

    @app.exception_handler(Exception)
    async def failed(_, exc):
        return JSONResponse({"error": "Executor unavailable; reconcile the original task"}, 503)

    @app.get("/v1/cluster")
    def cluster():
        return metadata(controller.root)

    @app.post("/v1/tasks", response_model=HostTaskObservation)
    def launch(task: HostTask):
        try:
            return controller.launch(task)
        except (ValueError, TransientProviderError) as error:
            raise AppError(
                409, "Host submission needs reconciliation; reuse the same task"
            ) from error

    @app.get("/v1/tasks/{identity}", response_model=HostTaskObservation)
    def observe(identity: str):
        try:
            return controller.observe(identity)
        except FileNotFoundError as error:
            raise AppError(404, "Unknown host task") from error
        except (ValueError, TransientProviderError) as error:
            raise AppError(409, "Host observation needs reconciliation") from error

    @app.middleware("http")
    async def no_retained_request_values(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def main():
    import uvicorn
    from common import state_home

    from platform_management.host_executor.controller import HostController
    from platform_management.integrations.cluster_client import ClusterClient

    root = state_home()
    # No caller-controlled API endpoint, namespace, token reference or command.
    client = ClusterClient(
        {
            "namespace": "platform-ops",
            "kube_ca_file": "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
            "kube_token_file": "/var/run/secrets/kubernetes.io/serviceaccount/token",
        }
    )
    inputs = json.loads((root / "cluster/executor-inputs.json").read_text())
    protected_ips = [
        p[k] for p in inputs["cpu_peers"].values() for k in ("public_ip", "private_ip")
    ]
    controller = HostController(
        root, os.environ["HOST_EXECUTOR_IMAGE"], client, protected_ips=protected_ips
    )
    metadata(root)  # Fail startup on a missing or conflicting retained identity.
    for filename in ("tls.crt", "tls.key", "ca.crt"):
        if not (TLS_HOME / filename).is_file():
            raise ValueError("Mutual TLS material is mandatory")
    uvicorn.run(
        create_app(controller),
        host="0.0.0.0",
        port=8443,
        ssl_keyfile=str(TLS_HOME / "tls.key"),
        ssl_certfile=str(TLS_HOME / "tls.crt"),
        ssl_ca_certs=str(TLS_HOME / "ca.crt"),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        ssl_version=ssl.PROTOCOL_TLS_SERVER,
        access_log=False,
        server_header=False,
        timeout_keep_alive=5,
        limit_concurrency=16,
    )


if __name__ == "__main__":
    main()
