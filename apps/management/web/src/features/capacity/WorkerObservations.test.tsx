import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { WorkerObservations } from './WorkerObservations';

describe('worker observations', () => {
  it('keeps missing measurements explicit', () => {
    const html = renderToStaticMarkup(
      <WorkerObservations
        members={[
          {
            provider_instance_id: 'synthetic',
            phase: 'joining',
            node_name: 'gpu-test',
            node_uid: 'node-test',
          },
        ]}
      />,
    );
    expect(html).toContain('No verified device observation yet.');
    expect(html).not.toContain('0 GiB');
  });

  it('shows observed usable memory and its timestamp without replacing advertised capacity', () => {
    const html = renderToStaticMarkup(
      <WorkerObservations
        members={[
          {
            provider_instance_id: 'synthetic',
            phase: 'gpu_ready',
            node_name: 'gpu-test',
            node_uid: 'node-test',
            gpu_observation: {
              observed_at: '2026-11-04T06:00:00Z',
              allocatable_gpu_count: 1,
              devices: [
                {
                  index: 0,
                  uuid: 'GPU-11111111-2222-3333-4444-555555555555',
                  name: 'NVIDIA A10',
                  memory_total_mib: 23028,
                  driver_version: '595.91.07',
                },
              ],
            },
          },
        ]}
      />,
    );
    expect(html).toContain('22.49');
    expect(html).toContain('Observed usable memory');
    expect(html).toContain('Device observation');
    expect(html).toContain('595.91.07');
  });
});
