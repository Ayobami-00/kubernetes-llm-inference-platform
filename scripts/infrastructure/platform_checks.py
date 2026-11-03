"""Render HA charts and check custom resources against checksum-pinned upstream CRDs.

Only --fetch-schemas uses the network. All rendered credentials and image identities
are synthetic test inputs; this command cannot install a release or call a cloud API.
Structural validation does not replace admission/CEL checks or live recovery tests.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

import jsonschema
import yaml
from common import ROOT, write_private

LOCK = ROOT / "deploy/platform-services.lock.json"
CACHE = ROOT / "artifacts/platform/schemas"
MAX_SCHEMA_BYTES = 4 * 1024 * 1024


def strict_properties(value):
    """Catch fields Kubernetes would otherwise prune; retain explicit free-form maps."""
    if isinstance(value, dict):
        result = {key: strict_properties(nested) for key, nested in value.items()}
        if (
            result.get("type") == "object"
            and "properties" in result
            and not result.get("x-kubernetes-preserve-unknown-fields")
        ):
            result.setdefault("additionalProperties", False)
        return result
    if isinstance(value, list):
        return [strict_properties(nested) for nested in value]
    return value


def load_schemas(*, fetch=False, cache=CACHE):
    schemas = {}
    for identity, item in json.loads(LOCK.read_text())["schemas"].items():
        filename = item["file"]
        if Path(filename).name != filename:
            raise ValueError("Schema cache paths must be plain filenames")
        path = cache / filename
        body = path.read_bytes() if path.is_file() else b""
        if hashlib.sha256(body).hexdigest() != item["sha256"]:
            if not fetch:
                raise ValueError(f"Missing or changed schema {filename}; use --fetch-schemas")
            if not item["url"].startswith("https://raw.githubusercontent.com/"):
                raise ValueError("Schema downloads must use the pinned upstream source")
            with urllib.request.urlopen(item["url"], timeout=30) as response:
                body = response.read(MAX_SCHEMA_BYTES + 1)
            if len(body) > MAX_SCHEMA_BYTES or hashlib.sha256(body).hexdigest() != item["sha256"]:
                raise ValueError(f"Schema size/checksum mismatch: {filename}")
            write_private(path, body.decode("utf-8"))
        document = yaml.safe_load(body)
        group, version, kind = identity.split("/")
        if document["spec"]["group"] != group or document["spec"]["names"]["kind"] != kind:
            raise ValueError("Pinned schema identity mismatch")
        schema = next(item for item in document["spec"]["versions"] if item["name"] == version)[
            "schema"
        ]["openAPIV3Schema"]
        schemas[identity] = strict_properties(schema)
    return schemas


def validate_custom_resources(resources, schemas):
    checked = 0
    for resource in resources:
        identity = resource["apiVersion"] + "/" + resource["kind"]
        if resource["apiVersion"].split("/")[0] not in {
            "postgresql.cnpg.io",
            "barmancloud.cnpg.io",
        }:
            continue
        if identity not in schemas:
            raise ValueError(f"No locked schema for {identity}")
        jsonschema.Draft7Validator(schemas[identity]).validate(resource)
        checked += 1
    if not checked:
        raise ValueError("No database custom resources were validated")
    return checked


def render_database(*, recovery=False):
    helm = ROOT / "artifacts/tools/bin/helm"
    executable = str(helm) if helm.is_file() else shutil.which("helm")
    if not executable:
        raise ValueError("Install Helm with make cluster-tools before platform checks")
    # Render-only fixtures. No value below is a deployable image, Secret or bucket.
    values = {
        "imageName": "fixture.invalid/postgres@sha256:" + "1" * 64,
        "storage.storageClass": "fixture-gp3",
        "walStorage.storageClass": "fixture-gp3",
        "applicationSecret": "fixture-app",
        "backup.destinationPath": "s3://fixture-backups/new",
        "backup.credentialsSecret": "fixture-aws",
    }
    if recovery:
        values.update(
            {
                "bootstrap.mode": "recovery",
                "bootstrap.recovery.destinationPath": "s3://fixture-backups/previous",
                "bootstrap.recovery.serverName": "previous",
            }
        )
    command = [
        executable,
        "template",
        "klip-db",
        str(ROOT / "charts/klip-database"),
        "--namespace",
        "klip",
        "--kube-version",
        json.loads(LOCK.read_text())["compatibility"]["kubernetes"],
    ]
    for key, value in values.items():
        command += ["--set-string", key + "=" + value]
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-schemas", action="store_true")
    args = parser.parse_args()
    schemas = load_schemas(fetch=args.fetch_schemas)
    for recovery in (False, True):
        resources = render_database(recovery=recovery)
        count = validate_custom_resources(resources, schemas)
        # Confirm the strict schema catches misspelled options rather than silently
        # pruning a durability policy. This is a schema check, never a live probe.
        invalid = copy.deepcopy(resources)
        next(item for item in invalid if item["kind"] == "Cluster")["spec"]["unknownPolicy"] = True
        try:
            validate_custom_resources(invalid, schemas)
        except jsonschema.ValidationError:
            pass
        else:
            raise ValueError("Custom-resource schema accepted an unknown policy field")
        print(f"{'Recovery' if recovery else 'Initialization'}: {count} custom resources validated")


if __name__ == "__main__":
    main()
