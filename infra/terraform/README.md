# Terraform ownership

`project.json` is the checked project/address allocation. Providers are configured
only in the three independent roots; modules use native AWS resources. Tool versions
and verified macOS ARM64/Linux AMD64 SHA-256 pins live in `toolchain.json`; each root
and module retains the same signed AWS provider lock for both platforms.

| Directory | Ownership |
| --- | --- |
| `state-bootstrap` | Durable state and adopted deployment access |
| `environments/aws-foundation` | AWS networking, CPU hosts/disks and private API NLB |
| `environments/inference-dns` | Exact inference A records and health checks |
| `modules/network` | VPC, subnets, routes and separate security-group rules |
| `modules/control-plane-compute` | Stable ENIs/EIPs, instances, volumes and SSM node identity |
| `modules/api-endpoint` | Internal TCP load balancing |
| `modules/inference-dns` | Existing-zone reference and weighted origin resources |

Use the repository Makefile/operator scripts and the provisioning/state/DNS/teardown
runbooks. Do not put credentials or private keys in Terraform. Runtime variables
are explicit operator inputs in private files; live account/region/AMI/address
defaults are versioned. The AMI is never advanced by a latest-image lookup.

`terraform-ref` is ignored reference material and has no deployment ownership.
There are no Terraform-owned Lambda instances, host provisioners, Kubernetes or
application releases. Infrastructure tests do not grant CI AWS deployment credentials.
