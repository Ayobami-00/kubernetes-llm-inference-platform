"""Live application placement, database TLS, HTTPS authentication and RWX checks."""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.cookiejar
import json
import socket
import ssl
import subprocess
import urllib.request
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse

from common import PROFILES, ROOT, environment, tool, utc_now, write_private
from image_release import guard_workloads
from management_release import ORIGIN, folder
from smoke import wait_until


class PinnedTunnelConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, local_port, **kwargs):
        if host != urlparse(ORIGIN).hostname:
            raise ValueError("Private HTTPS proof cannot redirect to another host")
        super().__init__(host, **kwargs)
        self.local_port = local_port

    def connect(self):
        if self._context.verify_mode != ssl.CERT_REQUIRED or not self._context.check_hostname:
            raise ValueError("Private HTTPS proof requires hostname and certificate verification")
        raw = socket.create_connection(("127.0.0.1", self.local_port), timeout=self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class PrivateHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context, port):
        super().__init__(context=context)
        self.port = port

    def https_open(self, request):
        return self.do_open(
            lambda host, **kw: PinnedTunnelConnection(host, local_port=self.port, **kw),
            request,
            context=self._context,
        )


@contextmanager
def forwarding(log):
    from cluster import FOLDER, kubeconfigs, tunnel

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    kubeconfigs()
    with tunnel(), log.open("w") as output:
        process = subprocess.Popen(
            [
                tool("kubectl"),
                "--kubeconfig",
                str(FOLDER / "operator.conf"),
                "-n",
                "klip",
                "port-forward",
                "service/klip-web",
                f"{port}:8443",
                "--address",
                "127.0.0.1",
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            env=environment(PROFILES["foundation"]),
        )
        try:

            def ready():
                if process.poll() is not None:
                    raise ValueError("Private application forwarding stopped")
                return f"Forwarding from 127.0.0.1:{port}" in log.read_text()

            wait_until(ready, "private HTTPS forwarding", timeout=30, interval=1)
            yield port
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def https_probe(port: int, ca_file) -> dict:
    jar = http.cookiejar.CookieJar()
    context = ssl.create_default_context(cafile=str(ca_file))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(jar),
        PrivateHTTPSHandler(context, port),
    )

    def request(path, body=None, csrf=None):
        headers = {"Origin": ORIGIN}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            ORIGIN + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        try:
            with opener.open(req, timeout=15) as response:
                data = response.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024:
                    raise ValueError("Application proof response exceeds its bound")
                return json.loads(data) if data else None
        except Exception:
            raise RuntimeError(
                "Authenticated HTTPS probe failed; credential-bearing details withheld"
            ) from None

    request("/health/ready")
    session = request(
        "/api/v1/auth/login",
        {
            "username": "admin",
            "password": (ROOT / "dev/.secrets/admin_password").read_text().strip(),
        },
    )
    try:
        if not jar or not all(cookie.secure for cookie in jar):
            raise ValueError("Production authentication must use Secure cookies")
        topology = request("/api/v1/topology")
        runs = request("/api/v1/runs")
        return {
            "verified_https": True,
            "hostname": urlparse(ORIGIN).hostname,
            "authenticated": True,
            "topology_nodes": len(topology["nodes"]),
            "historical_runs": len(runs),
            "cookie_secure": True,
        }
    finally:
        # No reusable cookie or CSRF token enters the verification bundle.
        request("/api/v1/auth/logout", {}, csrf=session["csrf_token"])


def verify() -> dict:
    from cluster import FOLDER, Host, kubectl, load_hosts
    from database_recovery import sql

    plan = json.loads((folder() / "plan.json").read_text())
    active = json.loads((folder() / "active.json").read_text())
    if active["approval_sha256"] != plan["approval_sha256"]:
        raise ValueError("Active management release differs from its verification plan")
    operation = uuid.uuid4().hex[:12]
    root = folder() / "verification" / operation
    report = {
        "schema_version": "klip-management-verification.v1",
        "operation": operation,
        "started_at": utc_now(),
        "release_approval": active["approval_sha256"],
        "image_source_commit": plan["image_assessment"]["source_commit"],
        "status": "running",
        "roles": {},
        "database_connections": {},
        "cleanup_pending": False,
    }
    path = root / "verification.json"
    write_private(path, report)
    backend_pods = []
    for role in ("api", "worker", "web"):
        deployment = json.loads(
            kubectl("-n", "klip", "get", "deployment", "klip-" + role, "-o", "json")
        )
        guard_workloads([deployment])
        status = deployment.get("status", {})
        expected = plan["image_assessment"]["images"]["web" if role == "web" else "backend"][
            "image"
        ]
        if (
            deployment["spec"]["replicas"] != 2
            or status.get("observedGeneration") != deployment["metadata"]["generation"]
            or any(
                status.get(k) != 2
                for k in ("readyReplicas", "availableReplicas", "updatedReplicas")
            )
            or deployment["spec"]["template"]["spec"]["containers"][0]["image"] != expected
        ):
            raise ValueError("Management replicas/generation/image are not ready")
        pods = json.loads(
            kubectl(
                "-n",
                "klip",
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/name=klip,app.kubernetes.io/component=" + role,
                "-o",
                "json",
            )
        )["items"]
        if (
            len(pods) != 2
            or len({p["spec"]["nodeName"] for p in pods}) != 2
            or any(
                not any(
                    c["type"] == "Ready" and c["status"] == "True"
                    for c in p.get("status", {}).get("conditions", [])
                )
                for p in pods
            )
            or any(p["spec"]["nodeName"] not in plan["targets"] for p in pods)
        ):
            raise ValueError("Management Pods are not Ready on distinct protected CPU hosts")
        report["roles"][role] = [
            {
                "name": p["metadata"]["name"],
                "uid": p["metadata"]["uid"],
                "node": p["spec"]["nodeName"],
                "image_ids": [c["imageID"] for c in p["status"]["containerStatuses"]],
            }
            for p in pods
        ]
        if role != "web":
            backend_pods.extend(p["metadata"]["name"] for p in pods)
        write_private(path, report)
    cp = Host(load_hosts()["cp-01"])
    prefix = ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "-n", "klip"]
    database_program = r"""
import json
from platform_management.settings import get_settings
from platform_management.db.session import engine
try:
 s=get_settings()
 assert s.database_require_tls and s.database_ca_file.is_file()
 with engine().connect() as db:
  query=('SELECT s.ssl,s.version,s.cipher,current_user AS role,'
         'current_database() AS database,r.rolsuper FROM pg_stat_ssl s '
         'JOIN pg_roles r ON r.rolname=current_user WHERE s.pid=pg_backend_pid()')
  r=db.exec_driver_sql(query).mappings().one()
  value=dict(r)
 assert value['ssl'] and not value['rolsuper'] and value['role']=='management'
 print(json.dumps(value))
except Exception:
 raise SystemExit('Application database TLS probe failed; details withheld')
"""
    for pod in report["roles"]["api"]:
        observed = cp.run(
            [*prefix, "exec", pod["name"], "--", "python", "-c", database_program], secret=True
        )
        report["database_connections"][pod["uid"]] = json.loads(observed)
        write_private(path, report)
    ca = base64.b64decode(
        cp.run(
            [*prefix, "get", "secret", "klip-web-ca", "-o", r"jsonpath={.data.tls\.crt}"],
            secret=True,
        ),
        validate=True,
    )
    if b"PRIVATE KEY" in ca or not ca.startswith(b"-----BEGIN CERTIFICATE-----"):
        raise ValueError("Expected a public management CA")
    ca_path = root / "management-ca.crt"
    write_private(ca_path, ca.decode())
    with forwarding(root / "forwarding.log") as port:
        report["https"] = https_probe(port, ca_path)
    if report["https"]["historical_runs"] != int(
        sql("klip-db", "SELECT count(*) FROM public.runs;")
    ):
        raise ValueError("Application run history does not match the retained database")
    canary = ".management-verification-" + operation
    marker = "synthetic-shared-storage-canary-" + operation
    writer = backend_pods[0]
    program = (
        "import os; from pathlib import Path; "
        f"p=Path('/artifacts')/{canary!r}; "
        "fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
        f"os.write(fd,{marker.encode()!r}); os.fsync(fd); os.close(fd)"
    )
    report["cleanup_pending"] = True
    write_private(path, report)
    try:
        kubectl("-n", "klip", "exec", writer, "--", "python", "-c", program)
        for pod in backend_pods:
            read = f"from pathlib import Path; print((Path('/artifacts')/{canary!r}).read_text())"
            if kubectl("-n", "klip", "exec", pod, "--", "python", "-c", read).strip() != marker:
                raise ValueError("Shared artifact canary differs")
        report["shared_artifacts"] = {
            "writer": writer,
            "readers": backend_pods,
            "sha256": hashlib.sha256(marker.encode()).hexdigest(),
        }
    finally:
        cleanup = (
            "from pathlib import Path; "
            + f"p=Path('/artifacts')/{canary!r}; "
            + f"assert not p.is_symlink() and p.read_text()=={marker!r}; p.unlink()"
        )
        kubectl("-n", "klip", "exec", writer, "--", "python", "-c", cleanup)
        report["cleanup_pending"] = False
        write_private(path, report)
    report.update(
        status="passed",
        verified_at=utc_now(),
        limitations=[
            "External frontend and public DNS are not tested by a private tunnel",
            "Replica/connection checks do not prove failure recovery",
            "Certificate rotation and sustained credentials still need live tests",
            "No GPU or real inference run is established by this check",
        ],
    )
    write_private(path, report)
    write_private(FOLDER / "management-verification.json", report)
    return report


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
