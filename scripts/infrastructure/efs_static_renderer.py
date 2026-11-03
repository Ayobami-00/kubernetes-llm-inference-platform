"""Helm post-renderer: retain only the reviewed static EFS node-driver resources.

The upstream chart renders controller RBAC even when controller.create=false.
No controller or dynamic provisioning is used by this platform.
"""

from __future__ import annotations

import sys

import yaml

KEEP = {
    ("DaemonSet", "efs-csi-node"),
    ("ServiceAccount", "efs-csi-node-sa"),
    ("ClusterRole", "efs-csi-node-role"),
    ("ClusterRoleBinding", "efs-csi-node-binding"),
    ("CSIDriver", "efs.csi.aws.com"),
}
DROP = {
    ("ServiceAccount", "efs-csi-controller-sa"),
    ("ClusterRole", "efs-csi-external-provisioner-role"),
    ("ClusterRole", "efs-csi-external-provisioner-role-describe-secrets"),
    ("ClusterRoleBinding", "efs-csi-provisioner-binding"),
    ("RoleBinding", "efs-csi-provisioner-binding-describe-secrets"),
}


def static_resources(rendered: str) -> str:
    retained = []
    seen = set()
    for item in yaml.safe_load_all(rendered):
        if not item:
            continue
        key = (item["kind"], item["metadata"]["name"])
        if key not in KEEP | DROP or key in seen:
            raise ValueError(
                "Unexpected/duplicate static EFS chart resource; review upstream change"
            )
        seen.add(key)
        if key in KEEP:
            retained.append(item)
    if not KEEP <= seen:
        raise ValueError("Static EFS chart is missing required node-driver resources")
    return yaml.safe_dump_all(retained)


if __name__ == "__main__":
    sys.stdout.write(static_resources(sys.stdin.read()))
