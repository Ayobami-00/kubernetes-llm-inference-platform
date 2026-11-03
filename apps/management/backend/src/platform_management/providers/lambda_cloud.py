"""Read-only Lambda API. Provider payloads are allowlisted before persistence."""

import time
from typing import Any

import httpx


class LambdaCloud:
    def __init__(self, key: str, transport=None, pause=time.sleep):
        self.client = httpx.Client(
            base_url="https://cloud.lambda.ai/api/v1/",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
            transport=transport,
        )
        self.pause = pause

    def observe(self) -> dict[str, Any]:
        try:
            response = self.client.get("instance-types")
            response.raise_for_status()
            types = []
            for value in response.json()["data"].values():
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
            instances, token = [], None
            while True:
                self.pause(1.05)
                response = self.client.get(
                    "instances",
                    params={"page_size": 100, **({"page_token": token} if token else {})},
                )
                response.raise_for_status()
                body = response.json()
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
                if not token:
                    break
            return {"instance_types": types, "instances": instances}
        finally:
            self.client.close()
