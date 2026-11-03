"""Authenticated stable core/v1 and batch/v1 client. Writes are restricted to Jobs."""

import os
import ssl
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from platform_management.providers.base import TransientProviderError


class ClusterClient:
    def __init__(self, binding, transport=None):
        self.binding = binding
        url = binding.get("kube_api_url") or (
            "https://"
            + os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
            + ":"
            + os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        )
        parsed = urlparse(url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (local and binding.get("kube_allow_insecure_local")):
            raise ValueError(
                "Cluster API requires HTTPS; HTTP is restricted to explicit local tests"
            )
        ca = Path(binding.get("kube_ca_file", "/missing"))
        verify = ssl.create_default_context(cafile=str(ca)) if ca.is_file() else True
        self.client = httpx.Client(
            base_url=url.rstrip("/") + "/", verify=verify, timeout=15, transport=transport
        )

    def request(self, method, path, body=None, missing=False, conflict=False):
        token_file = Path(self.binding.get("kube_token_file", "/missing"))
        headers = {"Content-Type": "application/merge-patch+json"} if method == "PATCH" else {}
        if token_file.is_file():
            headers["Authorization"] = "Bearer " + token_file.read_text().strip()
        try:
            response = self.client.request(method, path.lstrip("/"), json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise TransientProviderError("Cluster API transport failed") from exc
        if missing and response.status_code == 404:
            return None
        if conflict and response.status_code == 409:
            return None
        if response.status_code >= 500 or response.status_code in {408, 429}:
            raise TransientProviderError(f"Cluster API unavailable: HTTP {response.status_code}")
        if response.is_error:
            raise ValueError(f"Cluster API rejected request: HTTP {response.status_code}")
        return response.json() if response.content else {}

    def job_path(self, name=""):
        ns = quote(self.binding["namespace"], safe="")
        return f"apis/batch/v1/namespaces/{ns}/jobs" + ("/" + quote(name, safe="") if name else "")

    def job(self, name):
        return self.request("GET", self.job_path(name), missing=True)

    def pods(self, name):
        ns = quote(self.binding["namespace"], safe="")
        return self.request(
            "GET", f"api/v1/namespaces/{ns}/pods?labelSelector=job-name%3D{quote(name)}"
        )["items"]

    def close(self):
        self.client.close()
