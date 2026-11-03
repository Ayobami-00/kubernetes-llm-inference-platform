# State ownership and recovery

Three independent roots share `klip-tfstate-127811062476-eu-west-2`, in London:

| Root | Key | Identity |
| --- | --- | --- |
| state-bootstrap | `state-bootstrap/terraform.tfstate` | MFA-authenticated bootstrap user |
| aws-foundation | `aws-foundation/terraform.tfstate` | foundation deployment role |
| inference-dns | `inference-dns/terraform.tfstate` | DNS deployment role |

Each uses S3 native lock files (`use_lockfile = true`), versioning, AES256 server-side
encryption, owner enforcement and public-access blocking. The bucket policy denies
insecure transport. There is no DynamoDB lock table. Normal Terraform destruction
is blocked by `prevent_destroy`; `force_destroy` is false. Bootstrap IAM, state,
domain registration and the existing zone survive foundation teardown.

## First use

`make infra-bootstrap-plan` inspects the expected bucket owner and state before
initializing. With neither bucket nor state it generates one ignored
`local_backend_override.tf`, pointing to the persistent private operator directory
outside the worktree. It imports the existing role, five policies and four
attachments. Nothing is applied by planning.

After an authorized apply, the wrapper preserves a recovery copy, removes the local
override, performs explicit `init -migrate-state`, then downloads and verifies the
S3 state lineage and serial. Recovery state is retained under
`~/.local/state/kubernetes-llm-inference-platform/bootstrap/`. Preserve this private
directory until remote ownership has been independently confirmed. Never commit
state, saved plans or backend cache files.

## Reruns and interrupted migration

The bootstrap state classifier has these outcomes:

| Observation | Action |
| --- | --- |
| No bucket, no local/remote state | Start with private local state |
| Local state, no remote state | Resume that ownership; apply/reconcile, then migrate |
| Remote state exists, local absent or older with same lineage | Reuse S3 |
| Bucket exists, no ownership state | Stop; locate the original state before proceeding |
| Different lineage, newer local serial, or different content at equal serial | Stop for manual recovery |

For an interrupted first apply, rerun `make infra-bootstrap-plan`; do not delete its
local state. For an interrupted migration, use the same operator directory and
`python scripts/infrastructure/manage.py init --root bootstrap` to inspect/reselect the
backend, then the matching plan/apply workflow. If the apply completed locally and
only migration failed, run `python scripts/infrastructure/manage.py migrate-bootstrap` after
reviewing both states; it refuses conflicting ownership and retains recovery state.
The migration's `-force-copy` is used only when S3 has no bootstrap ownership state.
It is never a conflict-resolution strategy.

Before manual recovery, preserve both local files and the relevant S3 object
version IDs, compare lineages/serials/resources and verify the account. Do not use
`state push -force`, delete locks, import existing objects into a second root, or
reset state merely because initialization fails. Restore an S3 version only after
reviewing the resource ownership it represents and excluding concurrent operators.
Native locking prevents concurrent Terraform writes; it does not replace operator
coordination during migration and recovery.

Preview foundation/DNS plans have isolated empty local backends and cannot be
applied through the wrapper. Backend-free CI initialization and mocked tests use
temporary data directories and do not initialize operator state.

Official reference: [S3 backend and lock permissions](https://developer.hashicorp.com/terraform/language/backend/s3).
