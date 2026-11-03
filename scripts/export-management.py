"""Export OpenAPI from actual FastAPI routes; --check never writes."""

import argparse
import json
from pathlib import Path

from platform_management.api.app import app

parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
path = Path(__file__).resolve().parents[1] / "contracts/management.openapi.json"
content = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
if args.check:
    if not path.exists() or path.read_text() != content:
        raise SystemExit("Management OpenAPI drift; regenerate from routes.")
    print("Management OpenAPI is current.")
else:
    path.write_text(content)
