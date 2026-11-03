import json

import pytest
from fastapi.testclient import TestClient
from inference_stub.app import create_app
from workload_runner.datasets.public import openai_tools


def test_recorded_tool_schema_normalization_preserves_source():
    original = {
        "type": "function",
        "name": "read_file",
        "description": "Read",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
    before = json.dumps(original, sort_keys=True)
    tools, adapted = openai_tools([original])
    assert adapted and tools[0]["function"]["parameters"] == original["parameters"]
    assert json.dumps(original, sort_keys=True) == before
    assert openai_tools(tools) == (tools, False)
    with pytest.raises(ValueError, match="unsupported"):
        openai_tools([{"type": "unsupported"}])


def test_stub_rejects_non_openai_tool_envelopes():
    client = TestClient(create_app())
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "code-large",
            "messages": [{"role": "user", "content": "fixture"}],
            "tools": [{"type": "function", "name": "read_file"}],
        },
    )
    assert response.status_code == 400
