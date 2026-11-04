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
