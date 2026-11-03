"""OpenAI-compatible behavior simulator. Usage and responses are explicitly emulated."""

import argparse
import asyncio
import hashlib
import json
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


@dataclass
class StubSettings:
    first_content_ms: float = 5.0
    chunk_ms: float = 1.0
    slots_per_deployment: int = 2
    queue_limit: int = 8


def create_app(settings: StubSettings | None = None) -> FastAPI:
    settings = settings or StubSettings()
    queues = defaultdict(lambda: asyncio.Semaphore(settings.slots_per_deployment))
    pending = defaultdict(int)
    active = defaultdict(int)
    maximum_active = defaultdict(int)
    app = FastAPI(title="Inference behavior stub")

    @app.get("/health")
    def health():
        return {"fixture": True, "service": "inference-stub", "version": "0.1.0"}

    @app.get("/v1/models")
    def models():
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model"}
                for name in ("gec-small", "code-large", "chat-small", "general-medium", "platform")
            ],
        }

    @app.get("/debug/state")
    def queue_state():
        return {
            "fixture": True,
            "queues": [
                dict(
                    run_id=key[0],
                    deployment=key[1],
                    active=active[key],
                    pending=pending[key],
                    maximum_active=maximum_active[key],
                )
                for key in pending
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        if not body.get("messages") or any(
            tool.get("type") != "function"
            or not isinstance(tool.get("function"), dict)
            or not tool["function"].get("name")
            for tool in body.get("tools", [])
        ):
            return JSONResponse(
                {"error": "messages and OpenAI function tool envelopes are required"},
                status_code=400,
            )
        headers = request.headers
        task = headers.get("x-platform-task", "sentence_correction")
        preset = headers.get("x-platform-preset", "healthy")
        request_id = headers.get("x-platform-request-id", "fixture-request")
        key = (
            headers.get("x-platform-run-id", "standalone"),
            headers.get("x-platform-deployment", body.get("model", "default")),
        )
        if preset == "reject" or (
            preset == "shared-overload"
            and key[1] == "shared-medium"
            and int(hashlib.sha256(request_id.encode()).hexdigest()[:8], 16) % 3 == 0
        ):
            return JSONResponse({"error": {"message": "Synthetic overload"}}, status_code=429)
        if preset == "error":
            return JSONResponse({"error": {"message": "Synthetic failure"}}, status_code=503)
        if pending[key] >= settings.queue_limit:
            return JSONResponse({"error": {"message": "Synthetic queue full"}}, status_code=429)
        pending[key] += 1

        @asynccontextmanager
        async def slot():
            try:
                async with queues[key]:
                    active[key] += 1
                    maximum_active[key] = max(maximum_active[key], active[key])
                    try:
                        yield
                    finally:
                        active[key] -= 1
            finally:
                pending[key] -= 1

        text = str(body.get("messages", [{}])[-1].get("content", ""))
        if task == "sentence_correction":
            output = re.sub(r"\bShe go\b", "She goes", text).strip()
        elif task == "tutoring":
            output = "Which operation could help you find the next step?"
        elif task == "extraction":
            output = json.dumps(
                {
                    "clause": "Either party may terminate with thirty days notice.",
                    "evidence": "thirty days notice",
                }
            )
        elif task == "research":
            output = json.dumps({"answer": "Paris", "supporting_facts": [["France", 0]]})
        else:
            output = "Recorded inference response; no tools executed."
        words = re.findall(r"\S+\s*", output)
        maximum = int(body.get("max_tokens", body.get("max_completion_tokens", 128)))
        truncated = len(words) > maximum
        words = words[:maximum]
        output = "".join(words)
        finish = "length" if truncated else "stop"
        first_ms = settings.first_content_ms
        if preset == "slow-first":
            first_ms = max(first_ms, 1500.0)
        if preset == "timeout":
            first_ms = 300000.0
        completion_ms = 1500.0 if preset == "slow-completion" else 0
        usage = {
            "prompt_tokens": len(re.findall(r"\S+", json.dumps(body.get("messages", [])))),
            "completion_tokens": len(words),
            "prompt_tokens_details": {"cached_tokens": 0},
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        base = {"id": request_id, "model": body.get("model", "platform"), "created": 0}

        if body.get("stream", False):

            async def stream():
                async with slot():

                    def event(delta, reason=None, tokens=None):
                        value = {
                            **base,
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
                        }
                        if tokens is not None:
                            value["usage"] = tokens
                        return "data: " + json.dumps(value) + "\n\n"

                    yield event({"role": "assistant"})
                    await asyncio.sleep(first_ms / 1000)
                    for index, word in enumerate(words):
                        yield event({"content": word})
                        if preset == "interrupted" and index == 0:
                            return
                        await asyncio.sleep(settings.chunk_ms / 1000)
                    await asyncio.sleep(completion_ms / 1000)
                    yield event({}, finish, usage)
                    yield "data: [DONE]\n\n"

            return StreamingResponse(
                stream(), media_type="text/event-stream", headers={"X-Fixture": "true"}
            )
        async with slot():
            remaining = (first_ms + completion_ms) / 1000
            while remaining > 0:
                if await request.is_disconnected():
                    return JSONResponse(
                        {"fixture": True, "error": "client disconnected"}, status_code=499
                    )
                step = min(0.02, remaining)
                await asyncio.sleep(step)
                remaining -= step
            return {
                **base,
                "object": "chat.completion",
                "usage": usage,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": output},
                        "finish_reason": finish,
                    }
                ],
            }

    return app


app = create_app()


def main():
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--first-content-ms", type=float, default=5)
    parser.add_argument("--chunk-ms", type=float, default=1)
    parser.add_argument("--slots", type=int, default=2)
    args = parser.parse_args()
    uvicorn.run(
        create_app(StubSettings(args.first_content_ms, args.chunk_ms, args.slots)),
        host=args.host,
        port=args.port,
        timeout_graceful_shutdown=5,
    )
