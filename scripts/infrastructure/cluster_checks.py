"""Credential-free Ansible, Helm and release-lock checks."""

from __future__ import annotations

import json
import os
import platform
import subprocess

import yaml
from common import ROOT, tool
from platform_artifacts import LOCK, VERSIONS


def render_role_templates() -> None:
    from ansible.plugins.loader import init_plugin_loader
    from ansible.template import Templar, trust_as_template

    init_plugin_loader()
    variables = {
        "klip_node": {"role": "control_plane", "backup_recipient": "age1example"},
        "inventory_hostname": "cp-01",
        "item": {"item": "/example/path", "stat": {"exists": False}},
        "klip_repository": str(ROOT),
        "klip_peer_registry": "/example/peers.json",
        "klip_ca_bundle": "/example/ca.crt",
        "klip_firewall_file": "/example/firewall.rules",
        "klip_recovery_key_verified": True,
        "klip_runtime_images": {"stdout_lines": ["registry.k8s.io/pause:3.11"]},
        "klip_image_credentials_install_paths": [
            "/opt/klip/image-credentials/ecr-credential-provider"
        ],
        "klip_image_credentials_originals": {"results": []},
        "klip_image_credentials_backup": {"path": "/tmp/example"},
        "klip_image_credentials_binary": "/example/verified-binary",
        "klip_image_credentials_provider_yaml": "apiVersion: kubelet.config.k8s.io/v1\n",
        "index": 0,
    }
    templar = Templar(variables=variables)

    def visit(value):
        if isinstance(value, dict):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
        elif isinstance(value, str) and "{{" in value:
            templar.template(trust_as_template(value))

    for path in (ROOT / "infra/ansible/roles").rglob("*.yaml"):
        visit(yaml.safe_load(path.read_text()))


def main() -> None:
    lock = json.loads(LOCK.read_text())
    if lock["versions"] != VERSIONS:
        raise ValueError("Platform version lock drift")
    if yaml.safe_load((ROOT / "deploy/versions.yaml").read_text()) != VERSIONS:
        raise ValueError("Platform version summary drift")
    env = dict(os.environ)
    env["PATH"] = (
        str(ROOT / "artifacts/tools/bin")
        + os.pathsep
        + str(ROOT / ".venv/bin")
        + os.pathsep
        + env.get("PATH", "")
    )
    env["ANSIBLE_HOME"] = str(ROOT / "artifacts/ansible")
    env["ANSIBLE_CONFIG"] = str(ROOT / "infra/ansible/ansible.cfg")
    env["LC_ALL"] = "en_US.UTF-8" if platform.system() == "Darwin" else "C.UTF-8"
    env["LANG"] = env["LC_ALL"]
    os.environ.update({key: env[key] for key in ("ANSIBLE_HOME", "ANSIBLE_CONFIG")})
    render_role_templates()
    subprocess.run(
        [
            str(ROOT / ".venv/bin/ansible-inventory"),
            "--list",
            "-i",
            str(ROOT / "infra/ansible/inventory/example.yaml"),
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    for path in sorted((ROOT / "infra/ansible/playbooks").glob("*.yaml")):
        subprocess.run(
            [
                str(ROOT / ".venv/bin/ansible-playbook"),
                "--syntax-check",
                "-i",
                "localhost,",
                str(path),
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )
    subprocess.run(
        [str(ROOT / ".venv/bin/ansible-lint"), "--offline", str(ROOT / "infra/ansible/playbooks")],
        cwd=ROOT,
        env=env,
        check=True,
    )
    subprocess.run(
        [tool("helmfile"), "-f", "deploy/helmfile.yaml", "lint"], cwd=ROOT, env=env, check=True
    )
    rendered = subprocess.run(
        [tool("helmfile"), "-f", "deploy/helmfile.yaml", "template"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    documents = list(yaml.safe_load_all(rendered.stdout))
    for item in documents:
        if not item:
            continue
        spec = item.get("spec", {}).get("template", {}).get("spec")
        if spec and not any(
            taint.get("key") == "node-role.kubernetes.io/control-plane"
            for taint in spec.get("tolerations", [])
        ):
            raise ValueError(
                f"Platform Pod cannot schedule on the CPU nodes: {item['metadata']['name']}"
            )
        if item["kind"] == "CustomResourceDefinition":
            raise ValueError("CRDs must be installed explicitly, outside Helm releases")
        if item["kind"] == "CiliumClusterwideNetworkPolicy":
            policy = item["spec"]
            if policy.get("enableDefaultDeny") != {"ingress": False, "egress": False} or policy.get(
                "egressDeny"
            ) != [{"toCIDR": ["169.254.169.254/32"]}]:
                raise ValueError("Metadata guard must deny IMDS without isolating all Pod egress")
    (ROOT / "artifacts/helm-rendered.yaml").write_text(rendered.stdout)
    print(f"Rendered and checked {len(documents)} static release resources")
    from observability_release import render

    render()


if __name__ == "__main__":
    main()
