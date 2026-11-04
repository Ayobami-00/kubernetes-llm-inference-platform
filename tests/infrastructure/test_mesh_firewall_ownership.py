"""An omitted CPU handoff file cannot silently disable a new GPU's firewall."""

import pytest
import yaml
from common import ROOT


@pytest.mark.parametrize(
    "name,provider,file,allowed",
    [
        ("cp-01", "aws", False, True),
        ("platform-03", "aws", False, True),
        ("gpu-test", "lambda", False, False),
        ("cp-01", "lambda", False, False),
        ("arbitrary-host", "aws", False, False),
        ("gpu-test", "lambda", True, True),
    ],
)
def test_actual_ansible_condition_keeps_firewall_input_mandatory_for_gpu(
    name, provider, file, allowed, monkeypatch, tmp_path
):
    monkeypatch.setenv("ANSIBLE_HOME", str(tmp_path / "ansible"))
    pytest.importorskip("ansible")
    from ansible.plugins.loader import init_plugin_loader
    from ansible.template import Templar, trust_as_template

    init_plugin_loader()
    tasks = yaml.safe_load(
        (ROOT / "infra/ansible/roles/klip_wireguard/tasks/main.yaml").read_text()
    )
    variables = {"inventory_hostname": name, "klip_node": {"provider": provider}}
    if file:
        variables["klip_firewall_file"] = "/example/verified-firewall.rules"
    templar = Templar(variables=variables)
    condition = tasks[0]["ansible.builtin.assert"]["that"][0]
    assert templar.evaluate_conditional(trust_as_template(condition)) is allowed
    for task in tasks:
        if task["name"] in {
            "Install scoped firewall rules",
            "Install firewall service",
            "Apply dedicated firewall chains",
        }:
            assert templar.evaluate_conditional(trust_as_template(task["when"])) is file
