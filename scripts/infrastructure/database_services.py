"""Pinned operator configuration for the replicated, externally archived database."""

from __future__ import annotations


def values_for_operator(release: str, digest) -> dict:
    labels = {"app.kubernetes.io/instance": release}
    value = {
        "nodeSelector": {"klip.dev.node-restriction.kubernetes.io/platform": "true"},
        "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "1", "memory": "512Mi"},
        },
        "affinity": {
            "podAntiAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "topologyKey": "kubernetes.io/hostname",
                        "labelSelector": {"matchLabels": labels},
                    }
                ]
            }
        },
        "topologySpreadConstraints": [
            {
                "maxSkew": 1,
                "topologyKey": "topology.kubernetes.io/zone",
                "whenUnsatisfiable": "DoNotSchedule",
                "labelSelector": {"matchLabels": labels},
            }
        ],
    }
    if release == "cloudnative-pg":
        value.update(
            {
                "replicaCount": 2,
                "image": {
                    "tag": "1.30.1@" + digest("ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1")
                },
                "updateStrategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
                },
                "config": {
                    "data": {
                        "WATCH_NAMESPACE": "platform-data",
                        "POSTGRES_IMAGE_NAME": postgres_image(digest),
                    }
                },
            }
        )
    elif release == "plugin-barman-cloud":
        # CNPGI's gRPC runnable is leader-gated; the upstream readiness check only
        # tests the manager health port. Do not advertise an inactive replica as
        # a ready gRPC endpoint by increasing replicaCount without upstream support.
        value.update(
            {
                "replicaCount": 1,
                "image": {
                    "tag": "v0.15.1@" + digest("ghcr.io/cloudnative-pg/plugin-barman-cloud:v0.15.1")
                },
                "sidecarImage": {
                    "tag": "v0.15.1@"
                    + digest("ghcr.io/cloudnative-pg/plugin-barman-cloud-sidecar:v0.15.1")
                },
                # This manager is recoverable, not HA; existing WAL sidecars run in
                # each PostgreSQL Pod. Measure manager-loss/reconcile recovery live.
                "tolerations": [
                    {
                        "key": "node.kubernetes.io/" + key,
                        "operator": "Exists",
                        "effect": "NoExecute",
                        "tolerationSeconds": 30,
                    }
                    for key in ("not-ready", "unreachable")
                ],
            }
        )
    else:
        raise ValueError("Unreviewed database operator release")
    return value


def postgres_image(digest) -> str:
    image = "ghcr.io/cloudnative-pg/postgresql:18.6-standard-trixie"
    return image + "@" + digest(image)
