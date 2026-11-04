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

The operator workflow uses the reviewed backend CI image receipt. This CPU Job
does not depend on the runner, stub or web image; full management releases still
require their complete four-image assessment:

```sh
PYTHONPATH=scripts/infrastructure .venv/bin/python \
  scripts/infrastructure/lambda_provider_setup.py prepare --receipts <directory>
PYTHONPATH=scripts/infrastructure .venv/bin/python \
  scripts/infrastructure/lambda_provider_setup.py apply --approve-plan <printed-sha256>
PYTHONPATH=scripts/infrastructure .venv/bin/python \
  scripts/infrastructure/lambda_provider_setup.py observe --approve-plan <printed-sha256>
```

Preparation reads public identity and CPU addresses from the executor's retained
volume, validates the image/source/scan review, and binds live namespace, storage
and credential UIDs. Its resource envelope explicitly permits zero GPU instances.
It freezes the ConfigMap, Job and expected artifact path before dispatch. The Job
uses the existing application database/Secret references and client TLS leaf;
it has no host SSH keys, foundation AWS role, Kubernetes API token, host mounts
or GPU reservation. Kubernetes projected ConfigMap links are accepted only while
remaining within the mounted input directory.

There is one native Job per execution attempt, with no retry and a 180-second
deadline. Repeated `apply` calls cannot recreate it; `observe` binds its original
UID and validates the retained result. An uncertain dispatch or failed Job must
be reconciled before preparing a separately identified observation/recovery
attempt. The database intent survives such a recovery and prevents another POST
after an uncertain provider create. Frozen evidence is retained under private
`cluster/lambda-provider-setup/<plan-sha256>/` operator state and the application
artifact mount. This workflow does not enable management's paid lifecycle flag.

Official interfaces checked on 4 November 2026:
[Lambda API](https://docs.lambda.com/api/cloud) and
[firewall behavior](https://docs.lambda.ai/public-cloud/firewalls/). The transport
retains the existing fixed API origin and refuses redirects; this component does
not introduce a caller-selected provider endpoint.

## Activate the bounded management lifecycle

After the inspection-capable executor contains the exact GPU runtime review and
its cluster-bound approval has been staged, bind the existing chart configuration:

```sh
PYTHONPATH=scripts/infrastructure .venv/bin/python \
  scripts/infrastructure/lambda_lifecycle_activation.py \
  --provider-receipt <verified-provider-result.json>
```

This derives the SSH registration name and firewall ID from the verified free
setup, pins the previously checked plain Ubuntu image and creates an immutable
32-byte bootstrap seed Secret. Only its fingerprint and native UID enter the
operator receipt. The seed stays in private operator credentials and the
application's restricted Secret mount; never print it or commit it. Repeating
staging verifies and reuses the original seed/receipt. Different existing data,
an unresolved prerequisite, changed identities or missing runtime approval fail.

Staging does not change the running management release or allocate a GPU. The
next `management_release.py prepare/deploy` uses the staged lifecycle values and
rechecks executor/operator custody, seed UID and runtime approval at both phases.
Its saved plan includes this binding. Historical deployments without a binding
remain compatible while lifecycle is disabled. Keep the seed in the encrypted
recovery inventory before a destroy/rebuild rehearsal; a live Kubernetes Secret
alone is not a recovery backup.

The active policy permits only one `gpu_1x_a10` in Virginia, at most $1.29/hour,
90 initial minutes and 180 total minutes after an explicit renewal. It does not
authorize automatic substitution or larger-model capacity. The public UI/API
still owns the real create operation and its durable allocation receipt.
