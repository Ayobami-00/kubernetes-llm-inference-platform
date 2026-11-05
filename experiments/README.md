# Experiment records

## Real inference

- [WriterAssist on one A10: baseline vs n-gram](writer-assist-a10/README.md):
  measured latency, SLO attainment, GLEU, aggregate data and custom Matplotlib plots.
- [Detailed dashboard-run evidence](../docs/experiments/writer-assist-a10-dashboard-2026-11-04.md):
  immutable run identities, controls, stage timing, diagnostics and recovery acceptance.

## Public-input preparation and CPU acceptance

`public-preparation-samples.json` records the 100-input development samples used for CPU-only
acceptance: source/tokenizer revisions, upstream license evidence, preparation policies,
exclusions/adaptations and actual prepared-artifact checksums. It contains no corpus payloads or
machine-specific cache paths. These samples exercise preparation and the fixture endpoint; they
are not live model benchmarks or capacity measurements. Exgentic v2's CDLA-Permissive-2.0
declaration records the project owner's confirmation for the pinned revision. Its evidence is
identified as a project declaration because the upstream metadata field was unset when checked.

Full inputs remain in ignored `datasets/prepared/`; executions and reports remain in ignored
`artifacts/workloads/`. Use the documented smoke driver to reproduce preparation/execution paths.
The sampled upstream data and tokenizers are pinned in each preparation manifest; the development
sample includes bounded replay prefixes and explicit exclusions, not a representative production
trace. Live experiment summaries belong here with verified deployment versions and links
to retained artifacts; keep raw corpora and benchmark outputs outside Git.
