"""Stage the already cached public tokenizer; no downloads or host/GPU mutations."""

import argparse
import base64
import hashlib
import json
from pathlib import Path

from common import state_home, write_private
from huggingface_hub import try_to_load_from_cache
from platform_contracts import PreparationManifest
from retained_experiment import ARTIFACT_ROOT, api_exec
from retained_model import profile


def stage(evidence):
    source = state_home() / "datasets/writer-a10-nonthinking/public/writer-assist/latest.json"
    prepared = PreparationManifest.model_validate_json(source.read_text())
    model = profile("baseline").model
    if (
        prepared.tokenizer_id != model.repository
        or prepared.tokenizer_revision != model.tokenizer_revision
    ):
        raise ValueError("Preparation tokenizer differs from the pinned A10 profile")
    cached = try_to_load_from_cache(
        model.repository, "tokenizer.json", revision=model.tokenizer_revision
    )
    if not isinstance(cached, str):
        raise ValueError(
            "Prepare public JFLEG with the pinned tokenizer first; no implicit download"
        )
    content = Path(cached).read_bytes()
    destination = ARTIFACT_ROOT + "/prepared/tokenizer.json"
    metadata = {
        "repository": model.repository,
        "revision": model.tokenizer_revision,
        "source": f"https://huggingface.co/{model.repository}/blob/{model.tokenizer_revision}/tokenizer.json",
        "artifact": {"uri": "file://" + destination, "sha256": hashlib.sha256(content).hexdigest()},
    }
    files = {
        destination: content,
        ARTIFACT_ROOT + "/prepared/tokenizer-metadata.json": (
            json.dumps(metadata, sort_keys=True, indent=2) + "\n"
        ).encode(),
    }
    api_exec(
        """import sys,json,base64
from pathlib import Path
from platform_management.integrations.job_runner import frozen_file
for name,value in json.load(sys.stdin).items():
 p=Path(name)
 if not p.is_relative_to('/artifacts/experiments/retained-a10/prepared'):
  raise ValueError('Outside fixed preparation mount')
 frozen_file(p,base64.b64decode(value,validate=True))
print('Pinned public analysis tokenizer staged')
""",
        data=json.dumps({path: base64.b64encode(value).decode() for path, value in files.items()}),
    )
    write_private(evidence / "analysis-tokenizer.json", metadata)
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(stage(args.evidence.resolve()), indent=2))
