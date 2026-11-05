# Versioned configuration

| Directory | Contents |
| --- | --- |
| `customers/` | WriterAssist, CodeAgent, TutorChat, DocFlow and ResearchAgent requirements |
| `scenarios/` | Five baselines, shared contention and the five-customer mixed scenario |
| `worker-groups/` | Desired latency, shared and large-model capacity examples |
| `serving-profiles/` | Model, engine, GPU, placement and routing examples |

Load and validate these documents through `platform_contracts`. Stable IDs and
integer revisions are part of the interface. Published inputs and run snapshots
are immutable: create derived identities or new revisions for changes.

These authored examples include draft deployment pins. They are not automatically
executable against real infrastructure. Prepared artifacts, explicit serving
bindings, inventory, versions and live readiness are additional requirements.

See [customer mappings](customers/README.md),
[contract conventions](../packages/platform-contracts/README.md) and
[planning/execution](../docs/development/workloads.md).
