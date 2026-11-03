# Dataset manifests

This directory contains six authored source manifests, not benchmark payloads. Their `prepared`
fields remain `null`; Stream A creates separate prepared manifests with real checksums, record
counts, tokenizer revisions, template digests and input bounds under ignored cache storage.

See the [workload guide](../docs/development/workloads.md#public-preparation) for bounded public
preparation, separate synthetic corpora, upstream license evidence and CPU-only smoke runs.
Unknown source revisions or licenses also remain `null` rather than being fabricated.

| Manifest | Public source | Intended role |
| --- | --- | --- |
| `jfleg-dev` | [JFLEG](https://github.com/keisks/jfleg) | WriterAssist unscored warmup |
| `jfleg-test` | [JFLEG](https://github.com/keisks/jfleg) | WriterAssist correction evaluation |
| `exgentic-code` | [Exgentic agent LLM traces v2](https://huggingface.co/datasets/Exgentic/agent-llm-traces-v2) | SWE-bench-filtered coding inference calls |
| `mathdial` | [MathDial](https://github.com/eth-nlped/mathdial) | Tutoring dialogue proxy |
| `cuad` | [CUAD](https://www.atticusprojectai.org/cuad/) | Evidence-preserving clause extraction |
| `hotpotqa` | [HotpotQA](https://hotpotqa.github.io/) | Prepared multi-hop research QA substeps |

The [AIPerf dataset documentation](https://docs.nvidia.com/aiperf/reference/benchmark-datasets)
documents custom input and Exgentic replay. Our manifests describe adapter intentions; they are not
AIPerf configuration files. Exgentic's source revision is the documented v0.13.0 default and must be
checked against the eventual installed version. AIPerf is not a dependency of this package.

During the runner workstream, preparation must pin sources and splits, review licenses, preserve
record/session identities and references, record transformations and exclusion counts, reserve
disjoint warmup examples, and calculate real token lengths with the selected tokenizer/template.
Unsupported contexts must be filtered and reported rather than silently truncated. Shortening a
dataset or synthesizing text must be recorded as an adaptation, not attributed to private traffic.

JFLEG supplies sentences and multiple fluency references, not typing traces. MathDial pairs human
teachers with LLM-simulated students and covers math, rather than all educational interactions.
CUAD adaptation is document-text extraction, not OCR; changed/chunked inputs do not yield an
official CUAD benchmark score. HotpotQA provides answers and supporting facts, not a full web agent.
Exgentic replay preserves recorded contexts but executes no tools and does not incorporate live
answers into later recorded calls. It cannot establish functional coding-agent success.

Raw and prepared payloads belong under the ignored `raw/` and `prepared/` directories or in artifact
storage. Commit manifests and small, explicitly synthetic contract fixtures, not benchmark exports.
