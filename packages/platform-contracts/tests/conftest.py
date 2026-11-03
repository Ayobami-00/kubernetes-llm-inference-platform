import json
from pathlib import Path

import pytest
from platform_contracts import Run, load_catalog

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def root():
    return ROOT


@pytest.fixture
def catalog():
    return load_catalog(ROOT)


@pytest.fixture
def mixed_snapshot():
    return Run.model_validate_json(
        (ROOT / "contracts/fixtures/runs/mixed/platform-mixed-queued.json").read_text()
    ).snapshot


@pytest.fixture
def writer_run_data():
    return json.loads(
        (ROOT / "contracts/fixtures/runs/writer-assist/completed-passing.json").read_text()
    )
