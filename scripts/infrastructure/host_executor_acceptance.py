"""Prove the private service boundary from a credential-minimal management-namespace Job."""

import argparse
import json
import re
import time

from common import utc_now, write_private
from host_executor_release import folder
from image_release import guard_workloads

PROGRAM = r"""
import json,ssl,sys
from pathlib import Path
import httpx
origin='https://host-executor.platform-ops.svc:8443'
tls=Path('/run/secrets/host-client')
try:
    assert not Path('/run/secrets/host-executor/foundation-ssh-key').exists()
    context=ssl.create_default_context(cafile=str(tls/'ca.crt'))
    context.load_cert_chain(str(tls/'tls.crt'),str(tls/'tls.key'))
    with httpx.Client(verify=context,trust_env=False,follow_redirects=False,timeout=10) as client:
        good=client.get(origin+'/v1/cluster')
        assert good.status_code==200
        metadata=good.json()
        assert metadata['schema_version']=='klip-host-executor-metadata.v1'
        assert metadata['kube_system_uid']==sys.argv[1]
        assert metadata['cluster_id']=='klip'
        assert client.get(origin+'/openapi.json').status_code==404
        invalid=client.post(origin+'/v1/tasks',json={'unexpected':'untrusted-marker'})
        assert invalid.status_code==422 and 'untrusted-marker' not in invalid.text
    no_certificate=ssl.create_default_context(cafile=str(tls/'ca.crt'))
    missing_rejected=False
    try:
        with httpx.Client(verify=no_certificate,trust_env=False,timeout=5) as client:
            client.get(origin+'/v1/cluster')
    except httpx.HTTPError:
        missing_rejected=True
    wrong_ca=ssl.create_default_context()
    wrong_ca.load_cert_chain(str(tls/'tls.crt'),str(tls/'tls.key'))
    wrong_rejected=False
    try:
        with httpx.Client(verify=wrong_ca,trust_env=False,timeout=5) as client:
            client.get(origin+'/v1/cluster')
    except httpx.HTTPError:
        wrong_rejected=True
    assert missing_rejected and wrong_rejected
    print(json.dumps({'status':'verified','valid_client_accepted':True,
        'missing_client_certificate_rejected':missing_rejected,'wrong_server_ca_rejected':wrong_rejected,
        'invalid_task_rejected':True,'request_values_not_echoed':True,'no_ssh_keys_mounted':True,
        'kube_system_uid':metadata['kube_system_uid'],'registry_revision':metadata['registry_revision']}))
except Exception as error:
    print(json.dumps({'status':'failed','error_type':type(error).__name__}))
    sys.exit(1)
"""


def probe_job(identity, image, cluster_uid):
    labels = {
        "app.kubernetes.io/name": "klip-host-boundary-probe",
        "app.kubernetes.io/component": "worker",
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": identity, "namespace": "klip"},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 120,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": "klip-image-pull-probe",
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "nodeSelector": {"klip.dev.node-restriction.kubernetes.io/platform": "true"},
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 20002,
                        "runAsGroup": 20002,
                        "fsGroup": 20002,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "probe",
                            "image": image,
                            "command": ["python", "-c", PROGRAM, cluster_uid],
                            "env": [
                                {"name": "LANG", "value": "C.UTF-8"},
                                {"name": "LC_ALL", "value": "C.UTF-8"},
                                {"name": "LANGUAGE", "value": "C"},
                            ],
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "128Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {
                                    "name": "tls",
                                    "mountPath": "/run/secrets/host-client",
                                    "readOnly": True,
                                },
                                {"name": "temporary", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "tls",
                            "secret": {"secretName": "klip-host-client-tls", "defaultMode": 0o440},
                        },
                        {"name": "temporary", "emptyDir": {"sizeLimit": "64Mi"}},
                    ],
                },
            },
        },
    }


def verify(identity):
    from cluster import Host, kubectl, load_hosts
    from platform_management.integrations.job_runner import frozen_file
    from workload_acceptance import validate_preparation

    if not re.fullmatch(r"[a-f0-9]{12}", identity):
        raise ValueError("Use a distinct twelve-hex verification identity")
    active = json.loads((folder() / "active.json").read_text())
    stage = json.loads((folder() / (active["approval_sha256"] + ".stage.json")).read_text())
    directory = folder() / "acceptance" / identity
    report_path = directory / "result.json"
    if report_path.exists():
        return json.loads(report_path.read_text())
    cp = Host(load_hosts()["cp-01"])
    command = ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf"]
    pods = json.loads(
        kubectl(
            "-n",
            "platform-ops",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/component=api",
            "-o",
            "json",
        )
    )["items"]
    if (
        len(pods) != 2
        or len({p["spec"].get("nodeName") for p in pods}) != 2
        or any(
            not all(
                c["ready"] and c["imageID"].endswith(active["image"].split("@")[1])
                for c in p["status"].get("containerStatuses", [])
            )
            for p in pods
        )
    ):
        raise ValueError("Expected two ready reviewed executor API Pods on distinct CPU hosts")
    subject = "system:serviceaccount:platform-ops:klip-host-api"
    for verb, resource, namespace, expected in [
        ("create", "jobs.batch", "platform-ops", "yes"),
        ("get", "secrets", "platform-ops", "no"),
        ("create", "jobs.batch", "klip", "no"),
        ("patch", "nodes", None, "no"),
    ]:
        args = [*command, "auth", "can-i", verb, resource, "--as", subject]
        if namespace:
            args += ["-n", namespace]
        # auth can-i returns 1 for a correct denial; inspect that result safely.
        try:
            answer = cp.run(args).strip()
        except RuntimeError:
            if expected != "no":
                raise
            # Do not confuse transport failure with an authorization denial.
            answer = cp.run(
                [
                    "sudo",
                    "python3",
                    "-c",
                    "import json,subprocess,sys\n"
                    "r=subprocess.run(json.loads(sys.stdin.read()),capture_output=True,text=True)\n"
                    "print(json.dumps({'code':r.returncode,'answer':r.stdout.strip()}))",
                ],
                data=json.dumps(args),
            )
            observed = json.loads(answer)
            if observed != {"code": 1, "answer": "no"}:
                raise ValueError("Authorization denial could not be verified") from None
            answer = "no"
        if answer != expected:
            raise ValueError("Executor RBAC boundary differs")
    read_program = """import json,sys
from platform_management.integrations.cluster_client import ClusterClient
c=ClusterClient({'namespace':'platform-ops',
    'kube_ca_file':'/var/run/secrets/kubernetes.io/serviceaccount/ca.crt',
    'kube_token_file':'/var/run/secrets/kubernetes.io/serviceaccount/token'})
j=c.job(sys.argv[1])
print(json.dumps({'job_uid':j['metadata']['uid']}))
"""
    evidence = json.loads(
        cp.run(
            [
                *command,
                "-n",
                "platform-ops",
                "exec",
                pods[0]["metadata"]["name"],
                "--",
                "python",
                "-c",
                read_program,
                stage["job_name"],
            ]
        )
    )
    if evidence["job_uid"] != stage["job_uid"]:
        raise ValueError("API workload token reached an unexpected native Job")
    job = probe_job(
        "host-service-probe-" + identity, active["image"], stage["preflight"]["kube_system_uid"]
    )
    guard_workloads([job])
    manifest = directory / "job.json"
    had_manifest = manifest.exists()
    frozen_file(manifest, json.dumps(job, sort_keys=True).encode())
    existing = kubectl(
        "-n", "klip", "get", "job", job["metadata"]["name"], "--ignore-not-found", "-o", "json"
    )
    if existing.strip():
        observed = json.loads(existing)
        validate_preparation(observed, job)
    elif had_manifest:
        raise ValueError("Original boundary probe is missing; it cannot be recreated")
    else:
        observed = json.loads(
            cp.run(
                [*command, "-n", "klip", "create", "-f", "-", "-o", "json"], data=json.dumps(job)
            )
        )
    frozen_file(directory / "job-uid", observed["metadata"]["uid"].encode())
    deadline = time.monotonic() + 150
    while True:
        current = json.loads(
            kubectl("-n", "klip", "get", "job", job["metadata"]["name"], "-o", "json")
        )
        if current["metadata"]["uid"] != observed["metadata"]["uid"]:
            raise ValueError("Probe Job UID changed")
        done = {
            c["type"]
            for c in current.get("status", {}).get("conditions", [])
            if c["status"] == "True"
        }
        if done & {"Complete", "Failed"}:
            break
        if time.monotonic() > deadline:
            raise ValueError("Original boundary probe did not finish")
        time.sleep(1)
    output = kubectl("-n", "klip", "logs", "job/" + job["metadata"]["name"])
    result = json.loads(output.strip())
    if "Complete" not in done or result.get("status") != "verified":
        raise ValueError("Boundary probe failed; retained native Job was not restarted")
    result.update(
        verified_at=utc_now(),
        api_pods=[{"uid": p["metadata"]["uid"], "node": p["spec"]["nodeName"]} for p in pods],
        probe_job_uid=observed["metadata"]["uid"],
        native_api_token_verified=True,
        rbac_boundaries_verified=True,
    )
    write_private(report_path, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", required=True)
    print(json.dumps(verify(parser.parse_args().identity), indent=2))
