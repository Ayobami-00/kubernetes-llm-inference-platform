"""The retained experiment cannot turn arbitrary identities into filesystem/cloud work."""

from pathlib import Path

import pytest
import retained_experiment


@pytest.mark.parametrize("identity", ["a10-../escape", "a10-ok/other", "other-profile", "a10-"])
def test_invalid_experiment_identity_fails_before_any_host_inspection(monkeypatch, identity):
    def unexpected(*args):
        raise AssertionError("No host inspection or mutation is authorized")

    monkeypatch.setattr(retained_experiment, "checked_node", unexpected)
    with pytest.raises(ValueError, match="identity"):
        retained_experiment.stage(Path("unused"), "baseline", identity)


@pytest.mark.parametrize(
    "path",
    [
        "file:///etc/passwd",
        "/artifacts/other/run.json",
        "/artifacts/experiments/retained-a10/../elsewhere",
    ],
)
def test_artifact_reader_cannot_escape_the_retained_experiment(monkeypatch, path):
    def unexpected(*args, **kwargs):
        raise AssertionError("No cluster read is authorized")

    monkeypatch.setattr(retained_experiment, "api_exec", unexpected)
    with pytest.raises(ValueError):
        retained_experiment.read_shared(path)
