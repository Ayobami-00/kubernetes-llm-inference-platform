import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import type { Capacity } from '../../api/client';
import CapacityView from './CapacityView';

const observation: Capacity = {
  status: 'fresh',
  observed_at: new Date().toISOString(),
  instance_types: [
    {
      name: 'gpu_8x_b200',
      gpu_description: 'Fixture B200',
      price_cents_per_hour: 1999,
      specs: { gpus: 8, vcpus: 128, memory_gib: 1024 },
      regions_with_capacity_available: [],
    },
    {
      name: 'cpu_fixture',
      description: 'Fixture CPU',
      price_cents_per_hour: 0,
      specs: { gpus: 0, vcpus: 8, memory_gib: 32 },
      regions_with_capacity_available: [{ name: 'us-east-1' }],
    },
    {
      name: 'gpu_1x_h100',
      gpu_description: 'Fixture H100',
      price_cents_per_hour: 299,
      specs: { gpus: 1, vcpus: 26, memory_gib: 225 },
      regions_with_capacity_available: [{ name: 'us-west-1', description: 'California' }],
    },
  ],
  instances: [{ id: 'fixture-instance', name: 'Fixture account instance', status: 'active' }],
};
const render = (data: Capacity) =>
  renderToStaticMarkup(<CapacityView data={data} busy={false} refresh={() => {}} />);
describe('provider-reported capacity presentation', () => {
  it('puts available GPUs first, unavailable GPUs next, then CPU types and account inventory', () => {
    const html = render(observation);
    expect(html.indexOf('gpu_1x_h100')).toBeLessThan(html.indexOf('gpu_8x_b200'));
    expect(html.indexOf('gpu_8x_b200')).toBeLessThan(html.indexOf('cpu_fixture'));
    expect(html.indexOf('cpu_fixture')).toBeLessThan(html.indexOf('fixture-instance'));
    expect(html).toContain('is-unavailable');
    expect(html).toContain('No regions currently report capacity.');
    expect(html).toContain('$2.99');
    expect(html).toContain('USD / instance-hour');
    expect(html).toContain('System RAM');
    expect(html).toContain('225 GiB');
    expect(html).toContain('us-west-1');
    expect(html).toContain('not reserved capacity');
  });
  it.each(['stale', 'error'] as const)(
    'never labels retained %s observations as currently available',
    (status) => {
      const html = render({ ...observation, status, error: 'Fixture refresh failed' });
      expect(html).toContain('GPU availability unconfirmed');
      expect(html).toContain('Last reported regions');
      expect(html).toContain('us-west-1');
      expect(html).not.toContain('Available to rent');
      expect(html).not.toContain('data-availability="available"');
      expect(html).toContain('Previous observations are retained.');
    },
  );
  it('treats missing timestamps or regional data as unconfirmed', () => {
    expect(render({ ...observation, observed_at: null })).not.toContain('Available to rent');
    const html = render({
      ...observation,
      instance_types: [
        { name: 'gpu_fixture_unknown', specs: { gpus: 1 }, gpu_description: 'Unconfirmed fixture' },
      ],
    });
    expect(html).toContain('Unconfirmed');
    expect(html).toContain('Price not reported');
    expect(html).not.toContain('data-availability="unavailable"');
    expect(html).not.toContain('NaN');
  });
  it('expires retained availability even if the API payload still says fresh', () => {
    const html = render({
      ...observation,
      observed_at: new Date(Date.now() - 120_000).toISOString(),
    });
    expect(html).toContain('GPU availability unconfirmed');
    expect(html).not.toContain('Available to rent');
    const failedRequest = renderToStaticMarkup(
      <CapacityView
        data={observation}
        busy={false}
        refresh={() => {}}
        requestError="Fixture API offline"
      />,
    );
    expect(failedRequest).toContain('Fixture API offline');
    expect(failedRequest).not.toContain('data-availability="available"');
  });
  it('keeps empty regional capacity distinct from missing capacity', () => {
    const html = render({ ...observation, instance_types: [observation.instance_types![0]] });
    expect(html).toContain('No GPU capacity is currently reported.');
    expect(html).toContain('Unavailable GPUs');
    expect(html).not.toContain('GPU availability unconfirmed');
  });
  it('keeps connection setup separate from an observed empty catalogue', () => {
    const unconfigured = render({ status: 'unconfigured' });
    expect(unconfigured).toContain('Connect your Lambda account');
    expect(unconfigured).not.toContain('Unavailable GPUs');
    const empty = render({ ...observation, instance_types: [], instances: [] });
    expect(empty).toContain('No GPU instance types were reported');
    expect(empty).toContain('No account instances');
    expect(render(observation)).toContain('$0.00');
  });
});
