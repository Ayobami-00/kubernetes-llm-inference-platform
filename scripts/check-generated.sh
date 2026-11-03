#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --locked --package platform-contracts python scripts/export-contracts.py --check
