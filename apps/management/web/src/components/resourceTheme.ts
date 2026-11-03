import { Boxes, Cpu, Database, FlaskConical, Layers3, Network, Users } from 'lucide-react';
export const colours: Record<string, string> = {
  'dataset-manifest': 'var(--resource-dataset)',
  customer: 'var(--resource-customer)',
  'serving-profile': 'var(--resource-serving)',
  'worker-group': 'var(--resource-worker)',
  group: 'var(--resource-instance)',
  instance: 'var(--resource-instance)',
  scenario: 'var(--resource-scenario)',
  run: 'var(--resource-run)',
};
export const icons = {
  customer: Users,
  'dataset-manifest': Database,
  'serving-profile': Layers3,
  'worker-group': Cpu,
  group: Boxes,
  instance: Cpu,
  scenario: Network,
  run: FlaskConical,
};
export const names: Record<string, string> = {
  'dataset-manifest': 'Dataset',
  customer: 'Customer',
  'serving-profile': 'Serving',
  'worker-group': 'Worker profile',
  group: 'Worker group',
  instance: 'GPU instance',
  scenario: 'Scenario',
  run: 'Run',
};
