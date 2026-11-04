Lambda prerequisite setup registers the executor's existing **public** Ed25519
key and creates a dedicated Virginia bootstrap firewall ruleset. It cannot rent,
terminate, restart or modify an instance, replace an existing key/ruleset, or
change the account-wide firewall. All requests use the same PostgreSQL pacing
gate as management capacity refresh and real allocation.

The ruleset permits TCP/22 from the three platform CPU addresses and UDP/51820
from the six permanent CPU peers. These are explicit public `/32` sources, read
from the authoritative executor handoff. Public gateway ingress is a separate
serving activation step. Global firewall rules are observed and hashed; their
descriptions and other arbitrary provider fields are omitted from retained
results. Global rules may broaden provider ingress, so this setup does not
establish effective host isolation, networking, GPU readiness or gateway access.
The host firewall and later negative connectivity checks remain required.

The internal `PrerequisiteSpec` binds the Kubernetes namespace UID, six CPU
addresses, public key, Virginia region and provider credential fingerprint. It
contains no API key, private SSH key or callback seed. A distinct setup identity
is derived deterministically from those inputs. Resource names include cluster
and key/network fingerprints.

The existing management `preferences` table holds frozen input, account binding,
per-resource dispatch/ready/rejection states and observations. A session advisory
lock serializes setup ownership without an open transaction during HTTP. A
separate transaction commits `dispatching` before a create. Lost responses are
reconciled against exact name, identity, region, key and rules; an empty inventory
never authorizes another create. Preexisting names without a retained dispatch
intent are refused. Different pending setup inputs, account changes, resource
drift and definite rejections require explicit reconciliation.

The CPU Job entrypoint is:

```sh
python -m platform_management.jobs.provider_prerequisites \
  --spec /run/config/provider-setup.json \
  --output /artifacts/provider-setup/<setup-id>/<attempt-id>/result.json
```

It verifies the live executor identity over mutual TLS and resolves the provider
credential from its mounted Secret file. PostgreSQL uses the existing verified
TLS configuration. `--verify-only` requires an already-ready intent and performs
fresh provider reads without creating anything. Completed and failed attempt
artifacts are immutable; error output contains only exception types, never API
bodies or arbitrary exception messages. Provider private-key response fields
are not retained, and public-key registration always supplies `public_key`.

This is implementation documentation. A live setup is established only by its
native Job identity, committed PostgreSQL receipt and matching provider reads.
CPU-only unit tests exercise the actual bounded HTTP transport against synthetic
responses. Management PostgreSQL tests additionally cover concurrent setup and
committed intent before I/O; they run against the isolated CI test database.

Official interfaces checked on 4 November 2026:
[Lambda API](https://docs.lambda.com/api/cloud) and
[firewall behavior](https://docs.lambda.ai/public-cloud/firewalls/). The transport
retains the existing fixed API origin and refuses redirects; this component does
not introduce a caller-selected provider endpoint.
