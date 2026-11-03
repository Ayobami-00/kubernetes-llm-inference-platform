import json

import pytest
from fastapi.testclient import TestClient
from inference_stub.app import StubSettings, create_app


@pytest.mark.parametrize("streaming", [True, False])
def test_healthy_modes_and_emulated_usage(streaming):
    client = TestClient(create_app(StubSettings(first_content_ms=0, chunk_ms=0)))
    assert client.get("/health").json()["fixture"] is True
    response = client.post(
        "/v1/chat/completions",
        json=dict(
            model="gec-small",
            messages=[dict(role="user", content="She go to school.")],
            stream=streaming,
        ),
    )
    assert response.status_code == 200
    if streaming:
        chunks = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
        assert chunks[-1]["usage"]["completion_tokens"] == 4
        assert "[DONE]" in response.text
    else:
        assert response.json()["choices"][0]["message"]["content"] == "She goes to school."


@pytest.mark.parametrize(("preset", "status"), [("reject", 429), ("error", 503)])
def test_failure_presets(preset, status):
    client = TestClient(create_app())
    response = client.post(
        "/v1/chat/completions",
        headers={"x-platform-preset": preset},
        json=dict(model="gec-small", messages=[dict(role="user", content="hello")]),
    )
    assert response.status_code == status


def test_interrupted_stream_has_no_terminal_finish():
    client = TestClient(create_app(StubSettings(first_content_ms=0, chunk_ms=0)))
    response = client.post(
        "/v1/chat/completions",
        headers={"x-platform-preset": "interrupted"},
        json=dict(
            model="gec-small", messages=[dict(role="user", content="hello world")], stream=True
        ),
    )
    assert "[DONE]" not in response.text and '"finish_reason": "stop"' not in response.text
