/** Isolated component development only. Production entrypoint never installs these handlers. */
import { http, HttpResponse } from 'msw';
import type { Capacity, SpecView, Topology } from '../api/generated/types.gen';
export const handlers = [
  http.get('/api/v1/customers', () => HttpResponse.json<SpecView[]>([])),
  http.get('/api/v1/capacity', () =>
    HttpResponse.json<Capacity>({
      source: 'lambda',
      status: 'unconfigured',
      instance_types: [],
      instances: [],
    }),
  ),
  http.get('/api/v1/topology', () =>
    HttpResponse.json<Topology>({
      nodes: [],
      edges: [],
      historical: false,
      captured_at: new Date().toISOString(),
      counts: {},
    }),
  ),
];
