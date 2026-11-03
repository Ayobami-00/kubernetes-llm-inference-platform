"""Local textfile exporter: public peer metadata only; never read WireGuard private keys."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

DIRECTORY = Path("/var/lib/node_exporter/klip")
REGISTRY = Path("/etc/klip/peers.json")
NODE = Path("/etc/klip/node.json")


def command(field):
    result = subprocess.run(
        ["/usr/bin/wg", "show", "wg0", field],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode or len(result.stdout) > 128 * 1024:
        raise ValueError("WireGuard observation failed")
    values = {}
    for row in result.stdout.splitlines():
        parts = row.split()
        if len(parts) != (3 if field == "transfer" else 2) or parts[0] in values:
            raise ValueError("Malformed public WireGuard observation")
        numbers = [int(value) for value in parts[1:]]
        if any(value < 0 for value in numbers):
            raise ValueError("Negative WireGuard counter")
        values[parts[0]] = numbers
    return values


def render(registry, own_name, handshakes, transfers, stamp):
    peers = registry["peers"]
    if len(peers) > 256 or own_name not in peers:
        raise ValueError("Unexpected peer registry")
    lines = [
        "# HELP klip_wireguard_observation_success Whether the latest local observation succeeded.",
        "# TYPE klip_wireguard_observation_success gauge",
        "klip_wireguard_observation_success 1",
        "# TYPE klip_wireguard_observed_at_seconds gauge",
        f"klip_wireguard_observed_at_seconds {stamp}",
        "# TYPE klip_wireguard_peer_present gauge",
        "# TYPE klip_wireguard_last_handshake_seconds gauge",
        "# TYPE klip_wireguard_received_bytes_total counter",
        "# TYPE klip_wireguard_sent_bytes_total counter",
    ]
    expected = set()
    all_keys = [peer["public_key"] for peer in peers.values()]
    if len(set(all_keys)) != len(all_keys) or any(
        len(base64.b64decode(key, validate=True)) != 32 for key in all_keys
    ):
        raise ValueError("Invalid or duplicate public peer identities")
    for name, peer in sorted(peers.items()):
        if not re.fullmatch("[a-z0-9][a-z0-9.-]{0,62}", name):
            raise ValueError("Invalid peer name")
        if name == own_name:
            continue
        key = peer["public_key"]
        expected.add(key)
        label = f'peer="{name}"'
        present = key in handshakes and key in transfers
        lines.append(f"klip_wireguard_peer_present{{{label}}} {int(present)}")
        if key in handshakes:
            lines.append(f"klip_wireguard_last_handshake_seconds{{{label}}} {handshakes[key][0]}")
        if key in transfers:
            lines += [
                f"klip_wireguard_received_bytes_total{{{label}}} {transfers[key][0]}",
                f"klip_wireguard_sent_bytes_total{{{label}}} {transfers[key][1]}",
            ]
    lines += [
        "# TYPE klip_wireguard_unknown_peers gauge",
        f"klip_wireguard_unknown_peers {len((set(handshakes) | set(transfers)) - expected)}",
    ]
    return "\n".join(lines) + "\n"


def main():
    stamp = int(time.time())
    try:
        if REGISTRY.stat().st_size > 1024 * 1024 or NODE.stat().st_size > 1024 * 1024:
            raise ValueError("Local metadata exceeds bound")
        registry = json.loads(REGISTRY.read_text())
        own = json.loads(NODE.read_text())["name"]
        content = render(registry, own, command("latest-handshakes"), command("transfer"), stamp)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        content = (
            f"klip_wireguard_observation_success 0\nklip_wireguard_observed_at_seconds {stamp}\n"
        )
    DIRECTORY.mkdir(mode=0o755, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".wireguard-", suffix=".tmp", dir=DIRECTORY)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, DIRECTORY / "wireguard.prom")
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
