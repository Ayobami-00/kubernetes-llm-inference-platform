import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { execFileSync } from 'node:child_process';

const password = () =>
  process.env.MANAGEMENT_E2E_PASSWORD ||
  readFileSync(resolve('../../../dev/.secrets/admin_password'), 'utf8').trim();
test('graph, revision editing, durable allocation, serving, simulated run and reload', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await page.getByLabel('Password', { exact: true }).fill(password());
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await expect(page.getByRole('heading', { name: 'Your platform, connected.' })).toBeVisible();
  await page.getByRole('button', { name: 'Fit View', exact: true }).click();
  await expect(page.getByTestId('node-writer-assist').first()).toBeVisible();
  await page.getByTestId('node-writer-assist').first().click();
  await expect(page.getByRole('complementary', { name: 'Resource details' })).toContainText(
    'Service contract',
  );
  await page.getByRole('tab', { name: 'configure' }).click();
  await page.getByLabel('Customer name', { exact: true }).fill('WriterAssist');
  await page.getByRole('button', { name: 'Save draft', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('Draft saved');
  await page.getByRole('button', { name: 'Publish revision', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('Revision published');
  await page.getByRole('button', { name: 'Close details' }).click();
  await page.getByTestId('node-latency-workers').click();
  const groupName = 'browser-group-' + Date.now();
  await page.getByLabel('Group name', { exact: true }).fill(groupName);
  await page.getByRole('button', { name: 'Create simulated group', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('Worker group queued');
  await page.getByRole('button', { name: 'Close details' }).click();
  await page.getByRole('link', { name: 'Operations', exact: false }).click();
  await expect(page.locator('.operation-card').filter({ hasText: groupName })).toContainText(
    'gpu readiness',
    { timeout: 30000 },
  );
  execFileSync(
    'docker',
    ['compose', '-f', resolve('../../../dev/compose.yaml'), 'restart', 'worker'],
    { stdio: 'pipe' },
  );
  await page.reload();
  await expect(page.locator('.operation-card').filter({ hasText: groupName })).toContainText(
    'completed',
  );
  await page.getByRole('link', { name: 'Simulation lab' }).click();
  await page.getByRole('button').filter({ hasText: 'Qwen3-4B' }).click();
  await page.getByLabel('GPU-ready worker group').selectOption(groupName);
  const deploy = page.getByRole('button', { name: 'Deploy simulated serving' });
  // A prior smoke run may already own the seeded deployment identity.
  const prior = await page.request.get('/api/v1/serving-deployments');
  const deployed = (await prior.json()).find((d: { id: string }) => d.id === 'gec');
  if (!deployed) {
    await deploy.click();
    await expect(page.getByRole('status')).toContainText('Simulated deployment queued');
  }
  await page.getByRole('button', { name: 'Close details' }).click();
  await page.getByRole('button').filter({ hasText: 'Writer Assist Baseline' }).click();
  await page.getByLabel('Result preset').selectOption('violating');
  const launched = page.waitForResponse(
    (response) => response.url().endsWith('/api/v1/runs') && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: 'Simulate run', exact: true }).click();
  const launch = await launched;
  expect(launch.status(), await launch.text()).toBe(202);
  const identity = (await launch.json()).resource_id;
  await page.getByRole('button', { name: 'Close details' }).click();
  await page.getByRole('link', { name: 'Runs & results' }).click();
  const row = page.getByRole('button').filter({ hasText: identity });
  await expect(row).toContainText('violating', { timeout: 30000 });
  await row.click();
  await expect(page.getByRole('complementary', { name: 'Resource details' })).toContainText(
    'Synthetic fixture data',
  );
  await expect(page.getByRole('complementary', { name: 'Resource details' })).toContainText(
    'not evaluated',
  );
  await page.getByRole('button', { name: 'Explore frozen snapshot' }).click();
  await expect(page.getByRole('heading', { name: 'Frozen run snapshot' })).toBeVisible();
  await page.getByRole('button', { name: 'Return to current topology' }).click();
  await page.getByTestId('node-writer-assist').first().click();
  await page
    .getByRole('button', { name: 'Dismiss notification' })
    .click()
    .catch(() => {});
  await page.screenshot({
    path: '../../../artifacts/management/workspace-details.png',
    fullPage: true,
  });
  if (await page.getByRole('button', { name: 'Close details' }).isVisible())
    await page.getByRole('button', { name: 'Close details' }).click();
  await page.screenshot({
    path: '../../../artifacts/management/workspace.png',
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test('mobile navigation and insufficient-data history remain usable', async ({ page }) => {
  await page.setViewportSize({ width: 430, height: 932 });
  await page.goto('/');
  await page.getByLabel('Password', { exact: true }).fill(password());
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await page.getByRole('link', { name: 'Runs & results' }).click();
  const row = page
    .getByRole('button')
    .filter({ hasText: 'fixture-writer-assist-insufficient-data' });
  await row.click();
  await expect(page.getByRole('complementary', { name: 'Resource details' })).toContainText(
    'insufficient data',
  );
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
  ).toBeTruthy();
});
