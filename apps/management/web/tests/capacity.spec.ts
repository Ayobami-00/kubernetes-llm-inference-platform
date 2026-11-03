import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { Capacity } from '../src/api/generated/types.gen';

const password = () =>
  process.env.MANAGEMENT_E2E_PASSWORD ||
  readFileSync(
    process.env.MANAGEMENT_E2E_PASSWORD_FILE || resolve('../../../dev/.secrets/admin_password'),
    'utf8',
  ).trim();
for (const theme of ['dark', 'light'] as const) {
  test('KLIP branding and GPU availability in ' + theme + ' mode', async ({ page }) => {
    let observation: Capacity = {
      source: 'lambda',
      status: 'fresh',
      observed_at: new Date().toISOString(),
      instance_types: [
        {
          name: 'gpu_fixture_b200',
          gpu_description: 'Fixture B200',
          price_cents_per_hour: 1899,
          specs: { gpus: 8, memory_gib: 1024, vcpus: 128 },
          regions_with_capacity_available: [],
        },
        {
          name: 'cpu_fixture',
          description: 'Fixture CPU',
          price_cents_per_hour: 49,
          specs: { gpus: 0, memory_gib: 32, vcpus: 8 },
          regions_with_capacity_available: [{ name: 'us-east-1' }],
        },
        {
          name: 'gpu_fixture_h100',
          gpu_description: 'Fixture H100',
          price_cents_per_hour: 299,
          specs: { gpus: 1, memory_gib: 225, vcpus: 26 },
          regions_with_capacity_available: [{ name: 'us-west-1', description: 'California' }],
        },
      ],
      instances: [{ id: 'fixture-instance', name: 'Fixture account instance', status: 'active' }],
    };
    // Only provider observations are fixtures; login and the other workspace routes use the real API.
    await page.route('**/api/v1/capacity', (route) => route.fulfill({ json: observation }));
    await page.goto('/');
    await expect(page).toHaveTitle('KLIP · Kubernetes LLM Inference Platform');
    await expect(page.getByRole('heading', { name: 'Welcome to KLIP' })).toBeVisible();
    await page.getByRole('button', { name: 'Use ' + theme + ' theme' }).click();
    await page.getByLabel('Password', { exact: true }).fill(password());
    await page.getByRole('button', { name: 'Open workspace' }).click();
    await expect(page.getByRole('link', { name: 'KLIP home' })).toBeVisible();
    await expect(page.locator('.workspace-switch, .connection-card')).toHaveCount(0);
    await expect(page.locator('.navigation')).not.toContainText('Live inventory connected');
    await expect(page.locator('.navigation')).not.toContainText('Local workspace');
    await page.getByRole('link', { name: 'GPU capacity', exact: true }).click();
    await expect(page.locator('.summary-grid')).toHaveCount(0);
    const available = page.getByRole('region', { name: 'Available to rent' });
    await expect(available).toContainText('Fixture H100');
    await expect(available).toContainText('us-west-1');
    await expect(available).toContainText('$2.99');
    const unavailable = page.getByRole('article', { name: 'Fixture B200 · Unavailable' });
    await expect(unavailable).toHaveClass(/is-unavailable/);
    await expect(unavailable).toContainText('No regions currently report capacity.');
    const order = await page
      .locator('[data-instance-type]')
      .evaluateAll((cards) => cards.map((card) => card.getAttribute('data-instance-type')));
    expect(order).toEqual(['gpu_fixture_h100', 'gpu_fixture_b200', 'cpu_fixture']);
    await expect(page.getByRole('region', { name: 'Account instances' })).toContainText(
      'Fixture account instance',
    );
    const accessibility = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
      .analyze();
    expect(
      accessibility.violations.map((v) => ({
        id: v.id,
        nodes: v.nodes.map((n) => n.failureSummary),
      })),
    ).toEqual([]);
    await page.screenshot({
      path: '../../../artifacts/management/klip-capacity-' + theme + '.png',
      fullPage: true,
    });
    await page.setViewportSize({ width: 430, height: 932 });
    await expect(available).toBeVisible();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    ).toBeTruthy();
    await page.screenshot({
      path: '../../../artifacts/management/klip-capacity-mobile-' + theme + '.png',
      fullPage: true,
    });
    observation = {
      ...observation,
      status: 'fresh',
      observed_at: new Date(Date.now() - 120_000).toISOString(),
    };
    await page.reload();
    await expect(page.getByRole('heading', { name: 'GPU availability unconfirmed' })).toBeVisible();
    await expect(page.locator('[data-availability="available"]')).toHaveCount(0);
    observation = {
      ...observation,
      status: 'stale',
      error: 'Fixture refresh timeout',
      observed_at: new Date(Date.now() - 120000).toISOString(),
    };
    await page.reload();
    await expect(page.getByRole('heading', { name: 'GPU availability unconfirmed' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Available to rent', exact: true })).toHaveCount(
      0,
    );
    await expect(page.locator('[data-availability="available"]')).toHaveCount(0);
    await expect(page.locator('.capacity-page')).toContainText('us-west-1');
    await expect(page.getByRole('alert')).toContainText('Previous observations are retained');
  });
}
