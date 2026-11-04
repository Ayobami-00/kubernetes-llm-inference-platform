"""Management's mutually authenticated client; no SSH or Kubernetes authority."""

import ssl
from pathlib import Path

import httpx
from platform_contracts import HostTask, HostTaskObservation

from platform_management.providers.base import TransientProviderError

ORIGIN = "https://host-executor.platform-ops.svc:8443"


class HostExecutorClient:
    def __init__(self, tls_home: Path, *, transport=None):
        context = ssl.create_default_context(cafile=str(tls_home / "ca.crt"))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(tls_home / "tls.crt"), str(tls_home / "tls.key"))
        self.client = httpx.Client(
            base_url=ORIGIN,
            verify=context,
            transport=transport,
            timeout=20,
            trust_env=False,
            follow_redirects=False,
        )

    def request(self, method, path, body=None):
        try:
            with self.client.stream(method, path, json=body) as response:
                if response.status_code >= 500 or response.status_code in {408, 429}:
                    raise TransientProviderError("Host executor unavailable; observe original task")
                if response.status_code not in {200, 201}:
                    raise ValueError("Host executor rejected the fixed typed operation")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 32768:
                        raise ValueError("Executor response exceeds limit")
                import json

                return json.loads(payload)
        except httpx.HTTPError as error:
            raise TransientProviderError("Host transport failed; observe original task") from error

    def launch(self, task: HostTask):
        return HostTaskObservation.model_validate(
            self.request("POST", "/v1/tasks", task.model_dump(mode="json"))
        )

    def observe(self, identity):
        import re

        if not re.fullmatch(r"host-[a-f0-9]{24}", identity):
            raise ValueError("Invalid task identity")
        return HostTaskObservation.model_validate(self.request("GET", "/v1/tasks/" + identity))

    def cluster(self):
        return self.request("GET", "/v1/cluster")

    def close(self):
        self.client.close()
