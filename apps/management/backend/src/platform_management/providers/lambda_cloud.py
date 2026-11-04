"""Read-only Lambda API. Provider payloads are allowlisted before persistence."""

import time
from contextlib import nullcontext
from typing import Any

import httpx


class LambdaCloud:
    def __init__(self, key: str, transport=None, pause=time.sleep, *, gate=None):
        if not key or any(c in key for c in "\r\n"):
            raise ValueError("A valid provider credential file is required")
        if gate is None:
            if transport is None:
                from platform_management.providers.lambda_ledger import request_gate

                gate = request_gate
            else:

                def gate(**_):
                    return nullcontext()

        self.gate = gate
        self.client = httpx.Client(
            base_url="https://cloud.lambda.ai/api/v1/",
            headers={"Authorization": f"Bearer {key}", "Accept-Encoding": "identity"},
            timeout=15,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def read(self, path, *, params=None):
        if path not in {"instance-types", "instances"}:
            raise ValueError("Only the fixed provider capacity endpoints may be read")
        with self.gate(launch=False), self.client.stream("GET", path, params=params) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                data.extend(chunk)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError("Provider capacity response exceeds its bound")
        import json

        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("Provider capacity response is malformed")
        body = value.get("data")
        if path == "instances" and (
            not isinstance(body, list) or any(not isinstance(i, dict) for i in body)
        ):
            raise ValueError("Provider instance capacity response is malformed")
        if path == "instance-types" and (
            not isinstance(body, dict)
            or any(
                not isinstance(i, dict) or not isinstance(i.get("instance_type"), dict)
                for i in body.values()
            )
        ):
            raise ValueError("Provider instance type response is malformed")
        return value

    def observe(self) -> dict[str, Any]:
        try:
            response = self.read("instance-types")
            types = []
            for value in response["data"].values():
                item = value["instance_type"]
                types.append(
                    {
                        k: item[k]
                        for k in (
                            "name",
                            "description",
                            "gpu_description",
                            "price_cents_per_hour",
                            "specs",
                        )
                        if k in item
                    }
                    | {
                        "regions_with_capacity_available": value.get(
                            "regions_with_capacity_available", []
                        )
                    }
                )
            instances, token, seen = [], None, set()
            for _ in range(32):
                body = self.read(
                    "instances",
                    params={"page_size": 100, **({"page_token": token} if token else {})},
                )
                for item in body["data"]:
                    safe = {
                        k: item[k]
                        for k in ("id", "name", "ip", "private_ip", "status", "region")
                        if k in item
                    }
                    if "instance_type" in item:
                        safe["instance_type"] = {
                            k: item["instance_type"][k]
                            for k in ("name", "gpu_description", "price_cents_per_hour", "specs")
                            if k in item["instance_type"]
                        }
                    instances.append(safe)
                token = body.get("page_token")
                if token is None or token == "":
                    break
                if not isinstance(token, str) or token in seen:
                    raise ValueError("Provider capacity pagination did not advance")
                seen.add(token)
            else:
                raise ValueError("Provider capacity pagination exceeded its bound")
            identifiers = [i.get("id") for i in instances]
            if any(not isinstance(i, str) or not i for i in identifiers) or len(
                set(identifiers)
            ) != len(identifiers):
                raise ValueError("Provider capacity pagination omitted or duplicated identity")
            return {"instance_types": types, "instances": instances}
        finally:
            self.client.close()
