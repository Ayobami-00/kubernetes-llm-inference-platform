"""Read-only Kubernetes facts, cached outside request/database transactions."""

import os

from platform_management.db.models import Observation
from platform_management.db.session import session
from platform_management.integrations.cluster_client import ClusterClient
from platform_management.integrations.registry import configuration
from platform_management.services.common import now


def refresh_cluster():
    binding = configuration("real")
    if not binding["kube_api_url"] and not os.environ.get("KUBERNETES_SERVICE_HOST"):
        return
    client = None
    try:
        client = ClusterClient(binding)
        nodes = client.request("GET", "api/v1/nodes")["items"]
        ns = binding.get("serving_namespace", "llm-d")
        from urllib.parse import quote

        deployments = client.request(
            "GET", f"apis/apps/v1/namespaces/{quote(ns, safe='')}/deployments"
        )["items"]
        payload = {
            "status": "fresh",
            "observed_at": now().isoformat(),
            "error": None,
            "nodes": [
                {
                    "name": n["metadata"]["name"],
                    "uid": n["metadata"]["uid"],
                    "ready": any(
                        c["type"] == "Ready" and c["status"] == "True"
                        for c in n.get("status", {}).get("conditions", [])
                    ),
                    "gpus": int(
                        n.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu", "0")
                    ),
                }
                for n in nodes
            ],
            "deployments": [
                {
                    "id": d["metadata"]
                    .get("labels", {})
                    .get("klip.dev/deployment-id", d["metadata"]["name"]),
                    "uid": d["metadata"]["uid"],
                    "images": [
                        c["image"]
                        for c in d["spec"].get("template", {}).get("spec", {}).get("containers", [])
                    ],
                    "profile_sha256": d["metadata"]
                    .get("annotations", {})
                    .get("klip.dev/serving-profile-sha256"),
                    "profile_ref": d["metadata"]
                    .get("annotations", {})
                    .get("klip.dev/serving-profile"),
                    "replicas": d["spec"].get("replicas", 1),
                    "ready": d.get("status", {}).get("observedGeneration", 0)
                    >= d["metadata"].get("generation", 1)
                    and d["spec"].get("replicas", 1) > 0
                    and d.get("status", {}).get("availableReplicas", 0)
                    >= d["spec"].get("replicas", 1),
                }
                for d in deployments
            ],
        }
    except Exception:
        with session() as db:
            old = db.get(Observation, "cluster-readiness")
            payload = dict(old.payload) if old else {}
        payload.update(
            status="stale", error="Cluster observations unavailable; prior evidence retained"
        )
    finally:
        if client:
            client.close()
    with session() as db, db.begin():
        db.merge(Observation(id="cluster-readiness", payload=payload))
