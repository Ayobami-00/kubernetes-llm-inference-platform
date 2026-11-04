"""Install only the checksum-pinned kubectl used for limited gateway observation."""

import hashlib
import json
import platform
import sys
import urllib.request
from pathlib import Path


def install(lock, destination):
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise ValueError("The production backend build currently targets Linux amd64")
    item = json.loads(Path(lock).read_text())["artifacts"]["kubectl-linux-amd64"]
    with urllib.request.urlopen(item["url"], timeout=60) as response:
        content = response.read(100 * 1024 * 1024 + 1)
    if len(content) > 100 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != item["sha256"]:
        raise ValueError("Pinned kubectl download checksum differs")
    target = Path(destination)
    target.write_bytes(content)
    target.chmod(0o755)


if __name__ == "__main__":
    install(*sys.argv[1:])
