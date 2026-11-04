"""Targeted builds retain a fixed allowlist and the signed publishing prerequisite."""

import json
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("selected", ["all", "backend", "host-executor", "unknown"])
def test_component_selection_is_bounded_and_identity_gated(tmp_path, monkeypatch, selected):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/platform-images.yaml").read_text())
    jobs = workflow["jobs"]
    assert jobs["publish"]["needs"] == "identity"
    assert (
        "fromJSON(needs.identity.outputs.components)"
        in (jobs["publish"]["strategy"]["matrix"]["include"])
    )
    step = next(s for s in jobs["identity"]["steps"] if s.get("id") == "components")
    program = "\n".join(step["run"].splitlines()[1:-1])
    path = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    monkeypatch.setenv("SELECTED_COMPONENT", selected)
    if selected == "unknown":
        with pytest.raises(ValueError, match="Unknown build component"):
            exec(program, {})
        assert not path.exists()
    else:
        exec(program, {})
        value = json.loads(path.read_text().removeprefix("value="))
        assert len(value) == (5 if selected == "all" else 1)
        assert selected == "all" or value[0]["component"] == selected
        assert all(item["file"].endswith("Dockerfile") for item in value)
