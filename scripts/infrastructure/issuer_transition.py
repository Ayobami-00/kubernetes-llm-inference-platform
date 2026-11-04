"""Rolling, key-preserving service-account issuer transition for the existing kubeadm cluster."""

from __future__ import annotations

import hashlib
import json
import time
import uuid

import yaml
from common import CONFIG, PROFILES, Aws, source_digest, utc_now, write_private
from smoke import wait_until
from workload_identity import configuration, decoded, verify_public

OWNED_FLAGS = {"service-account-issuer", "service-account-jwks-uri", "api-audiences"}
MANIFEST = "/etc/kubernetes/manifests/kube-apiserver.yaml"


def restore_owned_manifest(host, original: str, candidate: str, attempted: bool) -> str:
    if not attempted:
        return "not_attempted"
    actual = host.run(["sudo", "sha256sum", MANIFEST]).split()[0]
    if actual == hashlib.sha256(candidate.encode()).hexdigest():
        host.put(MANIFEST, original)
        return "restored"
    if actual == hashlib.sha256(original.encode()).hexdigest():
        return "unchanged"
    return "blocked_by_drift"


def settings(primary: str) -> list[dict]:
    config = configuration()
    public, legacy = "https://" + config["issuer_hostname"], config["legacy_issuer"]
    if primary not in {public, legacy}:
        raise ValueError("Unknown primary service-account issuer")
    return [
        {"name": "service-account-issuer", "value": primary},
        {"name": "service-account-issuer", "value": legacy if primary == public else public},
        {"name": "service-account-jwks-uri", "value": public + "/openid/v1/jwks"},
        {"name": "api-audiences", "value": legacy + "," + public},
    ]


def transition_command(command: list[str], phase: str) -> list[str]:
    config = configuration()
    public, legacy = "https://" + config["issuer_hostname"], config["legacy_issuer"]
    if any(arg in {"--" + name for name in OWNED_FLAGS} for arg in command):
        raise ValueError("Identity flags must use the verified kubeadm --name=value form")
    issuers = [
        arg.split("=", 1)[1] for arg in command if arg.startswith("--service-account-issuer=")
    ]
    if not issuers or set(issuers) - {public, legacy} or len(issuers) != len(set(issuers)):
        raise ValueError("Unreviewed existing service-account issuer configuration")
    if phase not in {"accept", "primary"}:
        raise ValueError("Unknown issuer transition phase")
    for name, expected in (
        ("service-account-key-file", "/etc/kubernetes/pki/sa.pub"),
        ("service-account-signing-key-file", "/etc/kubernetes/pki/sa.key"),
    ):
        if [arg for arg in command if arg.startswith("--" + name + "=")] != [
            f"--{name}={expected}"
        ]:
            raise ValueError("The transition cannot change or reinterpret signing-key paths")
    audience = [arg.split("=", 1)[1] for arg in command if arg.startswith("--api-audiences=")]
    if len(audience) > 1 or (audience and set(audience[0].split(",")) - {legacy, public}):
        raise ValueError("Unreviewed existing API audiences; preserve them through explicit review")
    preserved = [
        arg for arg in command if arg.removeprefix("--").split("=", 1)[0] not in OWNED_FLAGS
    ]
    primary = public if phase == "primary" else issuers[0]
    desired = [f"--{arg['name']}={arg['value']}" for arg in settings(primary)]
    # Preserve exact command bytes on repeat, avoiding a needless static-Pod restart.
    current = [arg for arg in command if arg.removeprefix("--").split("=", 1)[0] in OWNED_FLAGS]
    return command[:] if current == desired else preserved + desired


def bootstrap_extra_args(existing: list[dict]) -> list[dict]:
    if not isinstance(existing, list) or any(set(arg) != {"name", "value"} for arg in existing):
        raise ValueError("Expected kubeadm v1beta4 extraArgs, preserving all unrelated flags")
    return [arg for arg in existing if arg["name"] not in OWNED_FLAGS] + settings(
        "https://" + configuration()["issuer_hostname"]
    )


def token_claims(token: str, issuer: str, audience: str, key_ids: list[str]) -> dict:
    try:
        header, payload, _ = token.strip().split(".")
        header, claims = json.loads(decoded(header)), json.loads(decoded(payload))
    except (ValueError, UnicodeError):
        raise ValueError("Unexpected service-account token encoding") from None
    audiences = claims.get("aud", [])
    if isinstance(audiences, str):
        audiences = [audiences]
    if (
        header.get("alg") != "RS256"
        or header.get("kid") not in key_ids
        or claims.get("iss") != issuer
        or audience not in audiences
    ):
        raise ValueError("Token issuer/audience/signer differs from the verified configuration")
    # Signature validation is performed by the live API TokenReview, not this parser.
    return {"issuer": claims["iss"], "audience": audience, "key_id": header["kid"]}


def activate() -> dict:
    import cluster
    from cluster import FOLDER, Host, load_hosts

    Aws(PROFILES["foundation"]).preflight("foundation")
    verified = verify_public()
    config = configuration()
    public, legacy = "https://" + config["issuer_hostname"], config["legacy_issuer"]
    hosts = load_hosts()
    name_list = sorted(CONFIG["nodes"])
    operation_id = uuid.uuid4().hex[:12]
    folder = FOLDER / "issuer-transitions" / operation_id
    report = {
        "schema_version": "klip-issuer-transition.v1",
        "status": "running",
        "operation_id": operation_id,
        "started_at": utc_now(),
        "source_sha256": source_digest(),
        "issuer": public,
        "key_ids": verified["key_ids"],
        "stages": [],
        "legacy_token_checked": False,
        "canary_token_expiry_seconds": 1800,
    }
    namespace = "klip-issuer-check-" + operation_id
    namespace_uid = None

    def save() -> None:
        write_private(folder / "observation.json", report)
        write_private(FOLDER / "issuer-transition.json", report)

    def api(name: str, *arguments: str, data=None, secret=False) -> str:
        return Host(hosts[name]).run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "--server=https://" + hosts[name]["klip_node"]["wireguard_ip"] + ":6443",
                "--request-timeout=5s",
                *arguments,
            ],
            data=data,
            secret=secret,
            timeout=15,
        )

    def healthy() -> None:
        health = Host(hosts[name_list[0]]).agent("etcd")
        if (
            len(health["members"]) != 3
            or len(health["health"]) != 3
            or not all(item["health"] for item in health["health"])
        ):
            raise ValueError("Three healthy etcd members are required before issuer changes")
        for name in name_list:
            if api(name, "get", "--raw", "/readyz").strip() != "ok":
                raise ValueError("All API servers must be ready before the next transition")

    def review(name: str, token: str, expected: bool = True) -> None:
        document = {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenReview",
            "spec": {"token": token, "audiences": [legacy]},
        }
        # TokenReview echoes spec.token. Keep the entire response in memory only.
        status = json.loads(
            api(name, "create", "-f", "-", "-o", "json", data=json.dumps(document), secret=True)
        )["status"]
        if status.get("authenticated", False) is not expected:
            raise ValueError("API token canary authentication did not match its expected audience")

    def mint(name: str, audience: str) -> str:
        return api(
            name,
            "-n",
            namespace,
            "create",
            "token",
            "issuer-canary",
            "--audience=" + audience,
            "--duration=30m",
            secret=True,
        ).strip()

    save()
    prior_deadline = cluster.OPERATION_DEADLINE
    cluster.OPERATION_DEADLINE = time.time() + 1800
    try:
        healthy()
        initial_config = json.loads(
            api(
                name_list[0],
                "-n",
                "kube-system",
                "get",
                "configmap",
                "kubeadm-config",
                "-o",
                "json",
            )
        )
        initial_bootstrap = yaml.safe_load(initial_config["data"]["ClusterConfiguration"])
        bootstrap_extra_args(initial_bootstrap.get("apiServer", {}).get("extraArgs", []))
        write_private(folder / "kubeadm-config-before.json", initial_config)
        documents = {}
        for name in name_list:
            content = Host(hosts[name]).run(["sudo", "cat", MANIFEST])
            documents[name] = yaml.safe_load(content)
            transition_command(documents[name]["spec"]["containers"][0]["command"], "accept")
            write_private(folder / f"{name}-initial.yaml", content)
        initial_primary = {
            name: next(
                arg.split("=", 1)[1]
                for arg in document["spec"]["containers"][0]["command"]
                if arg.startswith("--service-account-issuer=")
            )
            for name, document in documents.items()
        }
        ns = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": namespace,
                "annotations": {"klip.dev/verification-id": operation_id},
                "labels": {"pod-security.kubernetes.io/enforce": "restricted"},
            },
        }
        namespace_uid = json.loads(
            api(name_list[0], "create", "-f", "-", "-o", "json", data=json.dumps(ns))
        )["metadata"]["uid"]
        api(name_list[0], "-n", namespace, "create", "serviceaccount", "issuer-canary")
        legacy_node = next(
            (name for name, issuer in initial_primary.items() if issuer == legacy), None
        )
        old_token = mint(legacy_node, legacy) if legacy_node else None
        if old_token:
            token_claims(old_token, legacy, legacy, verified["key_ids"])
        for phase in ("accept", "primary"):
            for name in name_list:
                healthy()
                host = Host(hosts[name])
                original = host.run(["sudo", "cat", MANIFEST])
                document = yaml.safe_load(original)
                before = document["spec"]["containers"][0]["command"]
                desired = transition_command(before, phase)
                changed = desired != before
                document["spec"]["containers"][0]["command"] = desired
                desired_content = yaml.safe_dump(document)
                write_private(folder / f"{phase}-{name}-before.yaml", original)
                report["current_step"] = {"phase": phase, "node": name}
                save()
                attempted = False
                try:
                    if changed:
                        if (
                            host.run(["sudo", "sha256sum", MANIFEST]).split()[0]
                            != hashlib.sha256(original.encode()).hexdigest()
                        ):
                            raise ValueError("Static API manifest changed during issuer planning")
                        attempted = True
                        host.put(MANIFEST, desired_content)
                    observer = next(other for other in name_list if other != name)

                    def ready(observer=observer, name=name, desired=desired):
                        pod = json.loads(
                            api(
                                observer,
                                "-n",
                                "kube-system",
                                "get",
                                "pod",
                                "kube-apiserver-" + name,
                                "-o",
                                "json",
                            )
                        )
                        return (
                            pod["spec"]["containers"][0]["command"] == desired
                            and bool(pod["status"].get("containerStatuses"))
                            and all(c.get("ready") for c in pod["status"]["containerStatuses"])
                        )

                    wait_until(ready, "updated API server " + name, timeout=180, interval=3)
                    healthy()
                    if old_token:
                        for other in name_list:
                            review(other, old_token)
                        report["legacy_token_checked"] = True
                    report["stages"].append(
                        {
                            "phase": phase,
                            "node": name,
                            "changed": changed,
                            "api_ready": True,
                            "etcd_members": 3,
                        }
                    )
                    save()
                except Exception:
                    if changed:
                        report["rollback"] = {
                            "node": name,
                            "phase": phase,
                            "status": restore_owned_manifest(
                                host, original, desired_content, attempted
                            ),
                        }
                    raise
        for name in name_list:
            token = mint(name, legacy)
            token_claims(token, public, legacy, verified["key_ids"])
            for other in name_list:
                review(other, token)
        # STS-scoped credentials must not authenticate to the Kubernetes API audience.
        aws_token = mint(name_list[0], config["audience"])
        token_claims(aws_token, public, config["audience"], verified["key_ids"])
        for name in name_list:
            review(name, aws_token, expected=False)
        current = json.loads(
            api(
                name_list[0],
                "-n",
                "kube-system",
                "get",
                "configmap",
                "kubeadm-config",
                "-o",
                "json",
            )
        )
        bootstrap = yaml.safe_load(current["data"]["ClusterConfiguration"])
        existing = bootstrap.setdefault("apiServer", {}).get("extraArgs", [])
        bootstrap["apiServer"]["extraArgs"] = bootstrap_extra_args(existing)
        patch = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": current["metadata"]["resourceVersion"],
            },
            {
                "op": "replace",
                "path": "/data/ClusterConfiguration",
                "value": yaml.safe_dump(bootstrap),
            },
        ]
        api(
            name_list[0],
            "-n",
            "kube-system",
            "patch",
            "configmap",
            "kubeadm-config",
            "--type=json",
            "-p",
            json.dumps(patch),
        )
        report.update(
            status="completed",
            completed_at=utc_now(),
            bootstrap_config_updated=True,
            api_token_canaries_verified=True,
            sts_audience_isolated=True,
        )
    except Exception as error:
        report.update(status="failed", failed_at=utc_now(), error_code=type(error).__name__)
        raise
    finally:
        cluster.OPERATION_DEADLINE = prior_deadline
        if namespace_uid:
            report["cleanup_pending"] = True
            try:
                current = json.loads(api(name_list[0], "get", "namespace", namespace, "-o", "json"))
                if current["metadata"]["uid"] != namespace_uid:
                    raise ValueError("Issuer canary namespace identity changed; cleanup refused")
                api(name_list[0], "delete", "namespace", namespace, "--wait=false")
                wait_until(
                    lambda: (
                        not api(
                            name_list[0],
                            "get",
                            "namespace",
                            namespace,
                            "--ignore-not-found",
                            "-o",
                            "json",
                        ).strip()
                    ),
                    "issuer canary cleanup",
                    timeout=60,
                    interval=3,
                )
                report["cleanup_pending"] = False
            except Exception:
                report["status"] = "failed"
                raise
            finally:
                save()
        save()
    return report
