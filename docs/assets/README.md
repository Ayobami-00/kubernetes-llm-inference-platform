# Public documentation assets

`kubernetes-inference-platform.png` is the architecture image supplied for this
project, copied unchanged. It illustrates the broader platform. The root README
documents the private A10 measurement path and its deployment-specific differences.

`experiments/a10-*.png` and `experiments/a10-*.svg` are custom Matplotlib figures.
Their numerical source is the public aggregate result under
`experiments/writer-assist-a10/results/`; reproduce them from the repository root:

```bash
uv run --locked --group plots python scripts/plot_a10_results.py
```

Use PNGs in Markdown and SVGs for vector export. Keep the sample counts, units,
zero-based axes, measured quality change and one-pair limitation visible when
reusing the figures. Raw dataset records and captured model outputs are not
documentation assets and remain outside Git.
