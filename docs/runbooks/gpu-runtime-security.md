# GPU runtime evidence

The CPU executor review does not authorize the GPU host binaries or operator
operands. Retain native reports and compare exact versions and package findings
before admitting a candidate. A successful static scan does not establish CUDA,
kernel, networking or telemetry readiness.

Scan the checksum-pinned Linux containerd and runc binaries without executing
them or contacting a host:

```sh
PYTHONPATH=scripts/infrastructure .venv/bin/python \
  scripts/infrastructure/gpu_host_security.py \
  --directory artifacts/security/<new-host-scan>
```

`rootfs` mode is intentional: the executable-coverage assertion rejects an empty
filesystem scan. The report must recognize containerd, its runc shim, ctr and
runc. No ignore file suppresses findings. A cached runc is accepted only after
checking its locked checksum; otherwise the fixed public artifact is downloaded.
Downloads, archive bounds and extracted file digests are checked. Missing binary
coverage fails even if the scanner exits successfully.

To avoid duplicating the large vulnerability database, pass `--database-cache`
with a previously downloaded Trivy cache directory. Its native `UpdatedAt` must
be within 24 hours; a stale or future timestamp fails. The native database
metadata is retained in the summary. Each output directory must be new so earlier
failed or incomplete evidence survives.

On 4 November at 09:02:13 UTC, the corrected rootfs scan recognized the pinned
binaries and reported zero Critical and 12 unique High package/version tuples.
The earlier empty-coverage attempt and download failure are retained. Remaining
findings require applicability review; they have not been declared fixed. This
check did not allocate a GPU or execute any downloaded Linux binary.

The eight GPU Operator images have separate native image reports. In particular,
the driver report attributes kernel findings to its `linux-libc-dev` headers.
Any header-only applicability decision must verify the actual package file
inventory and report checksum. It cannot establish the running host kernel's
security or replace its independent version/configuration checks.

## Conditional single-A10 bootstrap review

`deploy/gpu-runtime-risk-review.json` binds all eight Linux AMD64 operand images,
host binaries, native report hashes, exact High/Critical finding sets, operator
values and network boundary. This seven-day review permits bootstrap/readiness
validation only. Public inference, arbitrary tenant images and subsequent
serving images remain outside its scope. Known kernel advisories remain a
limitation of this experimental baseline, rather than a production-security
claim. Raw reports are never edited or suppressed.

The driver's 22 Critical tuples and its header-attributed High tuples belong to
one `linux-libc-dev` package with 990 installed headers/documentation files. The
assessor verifies their complete inventory checksum and rejects any executable,
kernel image or changed file set. Non-header Critical findings always fail.
Other High findings retain component-specific applicability rationales and
limits. Upstream fixes should replace this temporary review when available.

The official Ubuntu `Ubuntu-6.8.0-147.147` source annotations explicitly disable
`CONFIG_GCOV_KERNEL` for AMD64. The executor additionally checks the actual booted
host configuration; source metadata cannot substitute for that observation.

After the executor image contains this exact review, prepare and stage its
cluster-bound receipt:

```sh
PYTHONPATH=scripts/infrastructure .venv/bin/python \
  scripts/infrastructure/gpu_runtime_approval.py prepare \
  --image-directory artifacts/security/<native-image-reports> \
  --host-directory artifacts/security/<native-host-report>
PYTHONPATH=scripts/infrastructure .venv/bin/python \
  scripts/infrastructure/gpu_runtime_approval.py apply --approve-plan <printed-sha256>
```

Preparation verifies fresh native evidence and freezes the cluster namespace,
executor image/custody identities, operator Deployment/ClusterPolicy UIDs and
policy-spec checksum. Staging requires both unchanged executor replicas and the
same operator identity. It holds the authoritative dispatch/peer-writer locks,
refuses unfinished tasks or allocated GPU peers, and writes immutable assessment
and approval files. The executor validates the actual retained assessment bytes,
not merely a supplied checksum string. A lost staging response requires explicit
receipt reconciliation; the helper never automatically repeats an attempt.
No GPU, host, Secret or network mutation is performed by this staging operation.
