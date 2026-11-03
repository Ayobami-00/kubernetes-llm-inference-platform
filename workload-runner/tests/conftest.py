from pathlib import Path

import pytest
from platform_contracts import ExecutionTarget, load_catalog
from workload_runner.datasets.preparation import prepare_customer
from workload_runner.scenario_loader import compile_run


@pytest.fixture
def repo():
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def catalog(repo):
    return load_catalog(repo)


@pytest.fixture
def prepared(catalog, tmp_path):
    return {
        ref: prepare_customer(catalog, ref, tmp_path / "prepared", limit=100)
        for ref in catalog.records["customer"]
    }


@pytest.fixture
def compile_scenario(catalog, prepared, tmp_path):
    def compile_(name="writer-assist-baseline", preset="healthy", output=None, smoke=True):
        return compile_run(
            catalog,
            name + "@1",
            prepared,
            ExecutionTarget(
                mode="local-stub",
                endpoint="http://127.0.0.1:8001/v1/chat/completions",
                stub_preset=preset,
            ),
            output or tmp_path / name,
            smoke=smoke,
        )

    return compile_
