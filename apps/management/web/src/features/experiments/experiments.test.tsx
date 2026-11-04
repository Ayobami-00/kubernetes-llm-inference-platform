import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ComparisonTable } from './ExperimentsPage';

describe('paired experiment result presentation', () => {
  it('keeps completed execution, invalid delivery and SLO verdict separate', () => {
    const html = renderToStaticMarkup(
      <ComparisonTable
        comparison={{
          baseline: {
            execution_status: 'completed',
            measurement_validity: 'valid',
            slo_verdict: 'violating',
            completion_latency: { samples: 200, p95_ms: 1200 },
            quality: { gleu: 0.42, coverage: { eligible: 220, evaluated: 200, failed: 20 } },
          },
          ngram: {
            execution_status: 'completed',
            measurement_validity: 'invalid',
            slo_verdict: 'not_evaluated',
            completion_latency: { samples: 200, p95_ms: null },
          },
        }}
      />,
    );
    expect(html).toContain('Measurement validity');
    expect(html).toContain('violating');
    expect(html).toContain('not_evaluated');
    expect(html).toContain('Unavailable');
    expect(html).toContain('Quality failures');
    expect(html).not.toContain('NaN');
    expect(html).not.toContain('passing');
  });
});
