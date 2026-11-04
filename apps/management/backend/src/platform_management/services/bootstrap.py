"""Bind launch-time cloud-init possession to a strict SSH host key.

Only an operation-scoped nonce reaches the VM. The seed remains a separate
mounted credential. PostgreSQL retains its hash/expiry and the public host key.
"""

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import yaml

from platform_management.db.models import ProviderIntent
from platform_management.db.session import session
from platform_management.services.common import AppError, now

SCRIPT_V1 = r"""import json, sys, time, urllib.error, urllib.request
from pathlib import Path

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

config_path = Path('/var/lib/klip/host-attestation.json')
try:
    config = json.loads(config_path.read_text())
    key = ' '.join(Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()[:2])
    body = json.dumps({'host_key': key}).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for attempt in range(30):
        request = urllib.request.Request(config['url'], data=body, method='POST', headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + config['nonce']})
        try:
            with opener.open(request, timeout=10) as response:
                if response.status == 200:
                    response.read(4096)
                    config_path.unlink()
                    sys.exit(0)
        except urllib.error.HTTPError as error:
            if error.code in (400, 401, 403, 409, 410, 422):
                break
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        if attempt < 29:
            time.sleep(10)
except (ValueError, KeyError, OSError):
    pass
print('KLIP host attestation failed; bootstrap remains quarantined', file=sys.stderr)
sys.exit(1)
"""


def public_host_key(value: str) -> str:
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise ValueError("Expected one Ed25519 public host key")
    fields = value.split()
    try:
        blob = base64.b64decode(fields[1], validate=True) if len(fields) == 2 else b""
    except ValueError:
        blob = b""
    if (
        len(fields) != 2
        or fields[0] != "ssh-ed25519"
        or len(blob) != 51
        or blob[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
    ):
        raise ValueError("Expected one Ed25519 public host key")
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


def fingerprint(key: str) -> str:
    blob = base64.b64decode(public_host_key(key).split()[1], validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def callback_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Host attestation requires the configured HTTPS management origin")
    return value.rstrip("/")


def nonce_for(seed: bytes, identity: str, origin: str, expires_at: str) -> str:
    if len(seed) != 32 or not re.fullmatch(r"op-[a-f0-9]{16,32}", identity):
        raise ValueError("Invalid bootstrap seed or operation identity")
    message = json.dumps(["klip-host-attestation.v1", identity, origin, expires_at]).encode()
    return hmac.new(seed, message, hashlib.sha256).hexdigest()


def cloud_init(identity: str, nonce: str, origin: str) -> str:
    if not re.fullmatch(r"op-[a-f0-9]{16,32}", identity) or not re.fullmatch(
        r"[a-f0-9]{64}", nonce
    ):
        raise ValueError("Invalid bootstrap identity or credential")
    origin = callback_origin(origin)
    config = json.dumps({"url": origin + "/api/v1/bootstrap/" + identity, "nonce": nonce})
    document = {
        "disable_root": True,
        "ssh_pwauth": False,
        "ssh_deletekeys": True,
        "ssh_genkeytypes": ["ed25519"],
        "write_files": [
            {"path": path, "owner": "root:root", "permissions": "0600", "content": content}
            for path, content in (
                ("/var/lib/klip/host-attestation.json", config),
                ("/var/lib/klip/attest-host.py", SCRIPT_V1),
            )
        ],
        "runcmd": [["python3", "/var/lib/klip/attest-host.py"]],
    }
    return "#cloud-config\n" + yaml.safe_dump(document, sort_keys=False)


def issue(identity: str, origin: str, seed_file):
    """Before dispatch, commit nonce metadata and reconstruct deterministic user data.

    Callers must not print or retain the returned user data in ordinary artifacts.
    The exact digest becomes part of the immutable Lambda launch plan.
    """
    origin = callback_origin(origin)
    if seed_file is None or not seed_file.is_file():
        raise AppError(422, "Bootstrap seed reference is unconfigured", "adapter_unavailable")
    seed = seed_file.read_bytes()
    if len(seed) != 32:
        raise AppError(422, "Bootstrap seed reference is invalid", "adapter_unavailable")
    with session() as db, db.begin():
        row = db.get(ProviderIntent, identity, with_for_update=True)
        if not row or row.payload.get("mode") != "real" or row.state != "pending":
            raise AppError(409, "Bootstrap requires an unattempted real intent", "bootstrap_state")
        existing = row.payload.get("bootstrap")
        seed_sha = hashlib.sha256(seed).hexdigest()
        if existing:
            if existing["origin"] != origin or existing["seed_sha256"] != seed_sha:
                raise AppError(409, "Bootstrap identity changed; reconcile the original intent")
            if datetime.fromisoformat(existing["expires_at"]) <= now():
                raise AppError(409, "Bootstrap credential expired", "bootstrap_expired")
            metadata = existing
        else:
            metadata = {
                "schema_version": "klip-host-attestation.v1",
                "origin": origin,
                "seed_sha256": seed_sha,
                "issued_at": now().isoformat(),
                "expires_at": (now() + timedelta(minutes=20)).isoformat(),
            }
        nonce = nonce_for(seed, identity, origin, metadata["expires_at"])
        metadata = {**metadata, "nonce_sha256": hashlib.sha256(nonce.encode()).hexdigest()}
        content = cloud_init(identity, nonce, origin)
        content_sha = hashlib.sha256(content.encode()).hexdigest()
        if metadata.get("cloud_init_sha256", content_sha) != content_sha:
            raise AppError(409, "Bootstrap program changed; reconcile the original intent")
        metadata["cloud_init_sha256"] = content_sha
        row.payload = {**row.payload, "bootstrap": metadata}
    return content, content_sha


def accept(db, identity: str, nonce: str, host_key: str):
    def authenticate(row):
        metadata = row.payload.get("bootstrap", {}) if row else {}
        valid = (
            row is not None
            and row.payload.get("mode") == "real"
            and row.state in {"dispatching", "allocated"}
            and re.fullmatch(r"[a-f0-9]{64}", nonce)
            and metadata.get("expires_at")
            and datetime.fromisoformat(metadata["expires_at"]) > now()
            and hmac.compare_digest(
                metadata.get("nonce_sha256", ""), hashlib.sha256(nonce.encode()).hexdigest()
            )
        )
        if not valid:
            raise AppError(
                401, "Bootstrap credential is invalid or expired", "bootstrap_unauthorized"
            )
        return metadata

    # Invalid guesses do not hold the intent's update lock or write attempt rows.
    authenticate(db.get(ProviderIntent, identity))
    row = db.get(ProviderIntent, identity, with_for_update=True, populate_existing=True)
    metadata = authenticate(row)
    key = public_host_key(host_key)
    if metadata.get("host_key") not in {None, key}:
        raise AppError(409, "Previously attested host key changed", "host_identity_conflict")
    observed = {
        **metadata,
        "host_key": key,
        "fingerprint": fingerprint(key),
        "accepted_at": metadata.get("accepted_at") or now().isoformat(),
    }
    row.payload = {**row.payload, "bootstrap": observed}
    return {"status": "accepted", "fingerprint": observed["fingerprint"]}
