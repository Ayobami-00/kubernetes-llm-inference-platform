"""Prove cert-manager leaf rotation reaches unchanged web Pods over verified TLS.

Manual issuance uses the same Issuing/ManuallyTriggered status transition as
https://github.com/cert-manager/cmctl/blob/v2.6.1/pkg/renew/renew.go, with additional
UID/resourceVersion JSON-Patch preconditions. No signing/private key is read.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import re
import ssl
import time
from urllib.parse import urlparse

from common import private_dir, utc_now, write_private
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from management_acceptance import PinnedTunnelConnection, forwarding, https_probe
from management_release import ORIGIN, folder
from smoke import wait_until


def public_identity(der: bytes) -> dict:
    cert = x509.load_der_x509_certificate(der)
    key = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return {
        "certificate_sha256": hashlib.sha256(der).hexdigest(),
        "public_key_sha256": hashlib.sha256(key).hexdigest(),
    }


def certificate():
    from cluster import kubectl

    return json.loads(kubectl("-n", "klip", "get", "certificate", "klip-web", "-o", "json"))


def issuing(value):
    return any(
        c["type"] == "Issuing" and c["status"] == "True"
        for c in value.get("status", {}).get("conditions", [])
    )


def public_secret(name):
    from cluster import kubectl

    return base64.b64decode(
        kubectl("-n", "klip", "get", "secret", name, "-o", r"jsonpath={.data.tls\.crt}"),
        validate=True,
    )


def web_pods():
    from cluster import kubectl

    pods = json.loads(
        kubectl(
            "-n",
            "klip",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=klip,app.kubernetes.io/component=web",
            "-o",
            "json",
        )
    )["items"]
    if len(pods) != 2 or any(
        not any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in p.get("status", {}).get("conditions", [])
        )
        for p in pods
    ):
        raise ValueError("Certificate rotation requires both web Pods Ready")
    return {
        p["metadata"]["name"]: {
            "uid": p["metadata"]["uid"],
            "images": {c["name"]: c["imageID"] for c in p["status"]["containerStatuses"]},
            "restarts": {c["name"]: c["restartCount"] for c in p["status"]["containerStatuses"]},
        }
        for p in pods
    }


def renewal_patch(value):
    if (
        value["metadata"]["name"] != "klip-web"
        or value["spec"]["secretName"] != "klip-web-tls"
        or issuing(value)
    ):
        raise ValueError("Certificate identity differs or issuance is already active")
    conditions = [c for c in value["status"]["conditions"] if c["type"] != "Issuing"]
    conditions.append(
        {
            "type": "Issuing",
            "status": "True",
            "reason": "ManuallyTriggered",
            "message": "Certificate re-issuance manually triggered",
            "observedGeneration": value["metadata"]["generation"],
            "lastTransitionTime": utc_now(),
        }
    )
    return [
        {"op": "test", "path": "/metadata/uid", "value": value["metadata"]["uid"]},
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": value["metadata"]["resourceVersion"],
        },
        {"op": "replace", "path": "/status/conditions", "value": conditions},
    ]


def prove(operation):
    from cluster import kubectl

    if not re.fullmatch(r"[0-9a-f]{12}", operation):
        raise ValueError("Use a stable 12-character lowercase hexadecimal operation")
    root = private_dir(folder() / "renewal" / operation)
    path = root / "verification.json"
    with (root / "operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            record = json.loads(path.read_text())
            if record["stage"] == "passed":
                return record
        else:
            value = certificate()
            patch = renewal_patch(value)
            old = x509.load_pem_x509_certificate(public_secret("klip-web-tls"))
            ca = public_secret("klip-web-ca")
            write_private(root / "ca.pem", ca.decode())
            record = {
                "operation": operation,
                "started_at": utc_now(),
                "stage": "prepared",
                "certificate_uid": value["metadata"]["uid"],
                "revision_before": value["status"]["revision"],
                "before": public_identity(old.public_bytes(serialization.Encoding.DER)),
                "ca_sha256": hashlib.sha256(ca).hexdigest(),
                "web_pods": web_pods(),
                "patch": patch,
            }
            write_private(path, record)
        if record["stage"] == "prepared":
            # Persist submission intent first. An ambiguous interrupted submission is
            # reconciled, never automatically submitted as a second renewal.
            record["stage"] = "triggering"
            write_private(path, record)
            kubectl(
                "-n",
                "klip",
                "patch",
                "certificate",
                "klip-web",
                "--subresource=status",
                "--type=json",
                "-p",
                json.dumps(record["patch"]),
            )
            record["stage"] = "triggered"
            write_private(path, record)
        else:
            live = certificate()
            if live["metadata"]["uid"] != record["certificate_uid"] or (
                not issuing(live) and live["status"]["revision"] <= record["revision_before"]
            ):
                raise ValueError("Ambiguous renewal submission; inspect before resubmitting")

        def renewed():
            value = certificate()
            if value["metadata"]["uid"] != record["certificate_uid"]:
                raise ValueError("Certificate was replaced during rotation")
            return (
                value
                if value["status"]["revision"] > record["revision_before"]
                and not issuing(value)
                and any(
                    c["type"] == "Ready" and c["status"] == "True"
                    for c in value["status"]["conditions"]
                )
                else None
            )

        value = wait_until(renewed, "cert-manager leaf reissuance", timeout=180, interval=2)
        fresh = x509.load_pem_x509_certificate(public_secret("klip-web-tls"))
        expected = public_identity(fresh.public_bytes(serialization.Encoding.DER))
        if (
            expected["public_key_sha256"] == record["before"]["public_key_sha256"]
            or hashlib.sha256(public_secret("klip-web-ca")).hexdigest() != record["ca_sha256"]
        ):
            raise ValueError("Leaf key did not rotate independently of its CA")
        record.update(revision_after=value["status"]["revision"], after=expected, handshakes={})
        write_private(path, record)
        context = ssl.create_default_context(cafile=str(root / "ca.pem"))
        for pod in record["web_pods"]:
            started = time.monotonic()
            with forwarding(root / (pod + ".log"), target="pod/" + pod) as port:

                def handshake():
                    connection = PinnedTunnelConnection(
                        urlparse(ORIGIN).hostname, local_port=port, context=context, timeout=10
                    )
                    try:
                        connection.connect()
                        actual = public_identity(connection.sock.getpeercert(binary_form=True))
                        return actual if actual == expected else None
                    finally:
                        connection.close()

                observed = wait_until(
                    handshake, "rotated web certificate handshake", timeout=240, interval=2
                )
                record["handshakes"][pod] = {
                    **observed,
                    "wait_seconds": round(time.monotonic() - started, 3),
                }
                write_private(path, record)
        if web_pods() != record["web_pods"]:
            raise ValueError("Web Pods restarted or were replaced; cache-only rotation is unproven")
        with forwarding(root / "authenticated.log") as port:
            record["https"] = https_probe(port, root / "ca.pem")
        record.update(stage="passed", completed_at=utc_now())
        write_private(path, record)
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", required=True)
    print(json.dumps(prove(parser.parse_args().operation), indent=2))


if __name__ == "__main__":
    main()
