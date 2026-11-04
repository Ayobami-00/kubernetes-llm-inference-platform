"""Prove the retained inference upstream timeout on every live Envoy replica.

The admin response stays in remote process memory. Only the fixed model cluster's
name and idle timeout values are returned; no listeners, headers or secrets persist.
"""

import json

from common import utc_now

PROGRAM = r"""
import json, subprocess, select, sys, urllib.request

pod = json.load(sys.stdin)["pod"]
if not pod.startswith("inference-") or "/" in pod:
    raise ValueError("Unexpected gateway")
p = subprocess.Popen(
    [
        "kubectl",
        "--kubeconfig=/etc/kubernetes/admin.conf",
        "-n",
        "llm-d",
        "port-forward",
        "--address=127.0.0.1",
        "pod/" + pod,
        ":19000",
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
)
try:
    if not select.select([p.stdout], [], [], 15)[0]:
        raise ValueError("No admin tunnel")
    line = p.stdout.readline()
    if not line.startswith("Forwarding from 127.0.0.1:"):
        raise ValueError("Unexpected bind")
    port = int(line.split("127.0.0.1:")[1].split(" ")[0])
    with urllib.request.urlopen(
        "http://127.0.0.1:" + str(port) + "/config_dump?resource=dynamic_active_clusters",
        timeout=20,
    ) as response:
        d = json.load(response)
    out = []

    def visit(v):
        if isinstance(v, list):
            for x in v:
                visit(x)
        elif isinstance(v, dict):
            if v.get("name") == "httproute/llm-d/writer-assist-a10/rule/0":
                out.append(v)
            else:
                for x in v.values():
                    visit(x)

    visit(d)

    def idle(v):
        if isinstance(v, dict):
            return [x for k, x in v.items() if k == "idle_timeout"] + sum(
                [idle(x) for x in v.values()], []
            )
        if isinstance(v, list):
            return sum([idle(x) for x in v], [])
        return []

    print(
        json.dumps(
            {
                "pod": pod,
                "matched_clusters": len(out),
                "http_idle_timeout": [idle(x) for x in out],
                "cluster_keys": [list(x) for x in out],
            }
        )
    )
finally:
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()

"""


def observe():
    from cluster import Host, kubectl, load_hosts

    pods = json.loads(
        kubectl(
            "-n",
            "llm-d",
            "get",
            "pods",
            "-l",
            "gateway.envoyproxy.io/owning-gateway-name=inference",
            "-o",
            "json",
        )
    )["items"]
    pods = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
    if len(pods) != 2 or len({p["spec"].get("nodeName") for p in pods}) != 2:
        raise ValueError("Two gateway replicas on separate CPU hosts are required")
    cp = Host(load_hosts()["cp-01"])
    rows = []
    for pod in pods:
        if not any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in pod["status"].get("conditions", [])
        ):
            raise ValueError("Gateway replica is not ready")
        proxy = next(c for c in pod["status"]["containerStatuses"] if c["name"] == "envoy")
        if not proxy["imageID"].endswith(
            "sha256:00c38048aeb4bb4b49d9ec6f3d7742a7c9f3d4d33f0371632a76734c02bebb7d"
        ):
            raise ValueError("Gateway proxy image differs from the reviewed pin")
        row = json.loads(
            cp.run(
                ["sudo", "python3", "-c", PROGRAM],
                data=json.dumps({"pod": pod["metadata"]["name"]}),
                timeout=60,
            )
        )
        if row["matched_clusters"] != 1 or row["http_idle_timeout"] != [["3s"]]:
            raise ValueError(
                "Effective gateway timeout differs; an accepted policy is insufficient"
            )
        rows.append(
            {
                "pod": row["pod"],
                "pod_uid": pod["metadata"]["uid"],
                "node": pod["spec"]["nodeName"],
                "http_idle_timeout": "3s",
            }
        )
    return {"observed_at": utc_now(), "replicas": rows}


if __name__ == "__main__":
    print(json.dumps(observe(), indent=2))
