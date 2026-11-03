# Experiment records

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
trace. Future live experiment summaries belong here with verified deployment versions and links
to retained artifacts; keep raw corpora and benchmark outputs outside Git.
