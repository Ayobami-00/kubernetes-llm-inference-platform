import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import ConfigurationDetails from './ConfigurationDetails';
import type { RunSnapshot } from '../api/client';

const snapshot = JSON.parse(
  readFileSync(
    resolve('../../../contracts/fixtures/runs/mixed/platform-mixed-queued.json'),
    'utf8',
  ),
).snapshot as RunSnapshot;
const show = (kind: string, document: unknown) =>
  renderToStaticMarkup(<ConfigurationDetails kind={kind} document={document} />);
describe('readable shared-contract specifications', () => {
  it('shows customer limits, SLO thresholds and quality intent without raw JSON', () => {
    const customer = snapshot.customers.find((c) => c.id === 'writer-assist')!;
    const html = show('customer', customer);
    expect(html).toContain('Service contract');
    expect(html).toContain('Rendered input ceiling');
    expect(html).toContain('512');
    expect(html).toContain('1,000');
    expect(html).toContain('95%');
    expect(html).toContain('Disabled');
    expect(html).toContain('Quality evaluation intent');
    expect(html).not.toContain('<pre');
    expect(html).not.toContain('&quot;max_output_tokens&quot;');
  });
  it('distinguishes unresolved worker allocation settings from zero values', () => {
    const html = show('worker-group', snapshot.worker_groups[0]);
    expect(html).toContain('Not configured');
    expect(html).toContain('Memory per GPU');
    expect(html).toContain('GiB');
    expect(html).toContain('first confirmed allocation');
    const zero = show('customer', { ...snapshot.customers[0], reserved_concurrency: 0 });
    expect(zero).toContain('>0<span');
  });
  it('keeps source provenance and preparation pending explicit', () => {
    const html = show(
      'dataset-manifest',
      snapshot.datasets.find((dataset) => !dataset.source.license)!,
    );
    expect(html).toContain('Not reviewed');
    expect(html).toContain('Preparation pending');
    expect(html).toContain('Known limitations');
    expect(html).toContain('rel="noreferrer noopener"');
    expect(html).not.toContain('<pre');
  });
  it('formats GPU memory budgets and unresolved version pins', () => {
    const html = show('serving-profile', snapshot.serving_profiles[0]);
    expect(html).toContain('80%');
    expect(html).toContain('Total configured demand');
    expect(html).toContain('Not configured');
    expect(html).toContain('Draft recipe');
  });
  it('preserves every participant and authored traffic phase', () => {
    const html = show('scenario', snapshot.scenario);
    for (const ref of snapshot.scenario.participants) expect(html).toContain(ref);
    expect(html).toContain('Measurement duration');
    expect(html).toContain('Poisson arrivals');
    expect(html).toContain('Warmup dataset overrides');
  });
  it('renders provider observations as labelled values rather than serialized objects', () => {
    const html = show('instance', {
      id: 'observed-gpu',
      ip: '192.0.2.1',
      region: { name: 'test-region' },
      instance_type: { name: 'gpu_1x_a100', specs: { gpus: 1, memory_gib: 64 } },
      ready: false,
    });
    expect(html).toContain('Public IP address');
    expect(html).toContain('192.0.2.1');
    expect(html).toContain('test-region');
    expect(html).toContain('gpu_1x_a100');
    expect(html).toContain('Disabled');
    expect(html).not.toContain('[object Object]');
    expect(html).not.toContain('<pre');
  });
});
