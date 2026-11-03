import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { Badge, RunDetails } from './Inspector';
import type { Run } from '../api/client';
const base = {
  id: 'fixture-test',
  fixture: true,
  status: 'completed',
  created_at: '2026-11-03T12:00:00Z',
  snapshot: {},
  result: {
    validity: 'valid',
    requests: {
      offered: 0,
      attempted: 0,
      completed: 0,
      rejected: 0,
      failed: 0,
      timed_out: 0,
      cancelled: 0,
      eligible_for_slo: 0,
    },
    customer_requests: {},
    measurements: [],
    slo_assessments: [],
    slo_verdict: 'insufficient_data',
  },
} as unknown as Run;
describe('historical result presentation', () => {
  it('keeps completed execution separate from insufficient data and quality', () => {
    const html = renderToStaticMarkup(<RunDetails run={base} />);
    expect(html).toContain('completed');
    expect(html).toContain('insufficient data');
    expect(html).toContain('not evaluated');
    expect(html).toContain('not measured performance');
    expect(html).not.toContain('passing');
  });
  it('renders a violating verdict without changing completed execution', () => {
    const html = renderToStaticMarkup(
      <RunDetails run={{ ...base, result: { ...base.result!, slo_verdict: 'violating' } }} />,
    );
    expect(html).toContain('completed');
    expect(html).toContain('violating');
  });
  it('uses explicit text for status rather than colour alone', () =>
    expect(renderToStaticMarkup(<Badge value="cleanup_pending" />)).toContain('cleanup pending'));
});

it('keeps external provenance and unknown usage explicit', () => {
  const run = {
    ...base,
    fixture: false,
    result: {
      ...base.result!,
      measurements: [
        {
          customer_ref: 'writer-assist@1',
          input_tokens: null,
          cached_input_tokens: null,
          output_tokens: null,
          latencies: {},
        },
      ],
    },
  } as unknown as Run;
  const html = renderToStaticMarkup(
    <RunDetails run={run} source="external-A" provenance="unverified" />,
  );
  expect(html).toContain('Unverified external report');
  expect(html).toContain('Not reported');
  expect(html).toContain('external-A');
  expect(html).not.toContain('Synthetic fixture data');
});
