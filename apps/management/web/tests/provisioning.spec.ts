import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { Capacity, WorkerLifecycleOptions } from '../src/api/generated/types.gen';

test('live provisioning uses the approved profile and rejects stale capacity', async ({ page }) => {
  // All provisioning endpoints in this browser test are intercepted. No allocation is sent to the API.
  let capacity: Capacity = {
    status: 'fresh',
    observed_at: new Date().toISOString(),
    instance_types: [
      {
        name: 'gpu_1x_a10',
        gpu_description: 'Fixture A10',
        price_cents_per_hour: 129,
        specs: { gpus: 1 },
        regions_with_capacity_available: [{ name: 'us-east-1' }],
      },
    ],
    instances: [],
  };
  const options: WorkerLifecycleOptions = {
    configured: true,
    instance_type: 'gpu_1x_a10',
    region: 'us-east-1',
    price_ceiling_cents_per_hour: 129,
    max_total_lease_minutes: 180,
    compatible_specs: [
      { spec_ref: 'latency-workers@2', worker_class: 'latency', lease_minutes: 90 },
    ],
  };
  const submitted: unknown[] = [];
  await page.route('**/api/v1/capacity', (route) => route.fulfill({ json: capacity }));
  await page.route('**/api/v1/worker-lifecycle-options', (route) =>
    route.fulfill({ json: options }),
  );
  await page.route('**/api/v1/worker-groups', (route) => {
    if (route.request().method() === 'POST') {
      submitted.push(route.request().postDataJSON());
      return route.fulfill({
        status: 202,
        json: { operation_id: 'fixture-operation', resource_id: 'browser-a10' },
      });
    }
    return route.fulfill({ json: [] });
  });
  await page.goto('/');
  const password =
    process.env.MANAGEMENT_E2E_PASSWORD ||
    readFileSync(
      process.env.MANAGEMENT_E2E_PASSWORD_FILE || resolve('../../../dev/.secrets/admin_password'),
      'utf8',
    ).trim();
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await page.getByRole('link', { name: 'GPU capacity', exact: true }).click();
  const form = page.getByRole('region', { name: 'Provision live Lambda worker' });
  await expect(form).toContainText('estimated $1.94');
  await form.getByLabel('Group name', { exact: true }).fill('browser-a10');
  const submit = form.getByRole('button', { name: 'Provision worker', exact: true });
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect.poll(() => submitted.length).toBe(1);
  expect(submitted[0]).toEqual({
    id: 'browser-a10',
    spec_ref: 'latency-workers@2',
    mode: 'real',
    fault: 'none',
  });
  capacity = { ...capacity, observed_at: new Date(Date.now() - 120_000).toISOString() };
  await page.reload();
  await form.getByLabel('Group name', { exact: true }).fill('browser-a10-stale');
  await expect(submit).toBeDisabled();
  await expect(form).toContainText('Refresh provider capacity before provisioning.');
  expect(submitted).toHaveLength(1);
});
