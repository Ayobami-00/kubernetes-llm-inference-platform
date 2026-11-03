import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';

test('import a derived workload bundle, launch the actual runner and download its report', async ({
  page,
}) => {
  test.skip(
    !process.env.MANAGEMENT_WORKLOAD_PLAN_FILE,
    'The actual workload integration installs its own runner and stub',
  );
  const password = readFileSync(process.env.MANAGEMENT_E2E_PASSWORD_FILE!, 'utf8').trim();
  const snapshot = JSON.parse(readFileSync(process.env.MANAGEMENT_WORKLOAD_PLAN_FILE!, 'utf8'));
  const id = `browser-workload-${Date.now()}`;
  await page.goto('/');
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await page.getByRole('link', { name: 'Imports', exact: true }).click();
  await page.getByLabel('Document type').selectOption('prepared-runs');
  await page.getByLabel('Source name').fill('Browser actual workload proof');
  await page.getByLabel('Source provenance').selectOption('synthetic');
  await page.getByLabel('Import JSON file').setInputFiles({
    name: 'prepared.json',
    mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify({ id, fixture: true, snapshot })),
  });
  await page.getByRole('button', { name: 'Confirm import', exact: true }).click();
  await expect(page.locator('.import-receipt').filter({ hasText: id }).first()).toContainText(
    'completed',
    { timeout: 30000 },
  );
  await page.getByRole('link', { name: 'Simulation lab', exact: true }).click();
  await page.getByRole('button').filter({ hasText: 'writer-assist-baseline' }).first().click();
  const inspector = page.getByRole('complementary', { name: 'Resource details' });
  await inspector.getByRole('tab', { name: 'overview' }).click();
  await inspector.getByLabel('Prepared run bundle').selectOption(id);
  await inspector.getByRole('button', { name: 'Check readiness', exact: true }).click();
  const launch = inspector.getByRole('button', { name: 'Run local workload', exact: true });
  await expect(launch).toBeEnabled();
  const responsePromise = page.waitForResponse(
    (r) => r.url().endsWith('/api/v1/runs') && r.request().method() === 'POST',
  );
  await launch.click();
  const accepted = await (await responsePromise).json();
  const runId = accepted.resource_id;
  await page.getByRole('button', { name: 'Close details' }).click();
  await page.getByRole('link', { name: 'Runs & results', exact: true }).click();
  const row = page.getByRole('button').filter({ hasText: runId });
  await expect(row).toContainText('completed', { timeout: 60000 });
  await row.click();
  await expect(inspector).toContainText('Synthetic fixture data');
  const downloadPromise = page.waitForEvent('download');
  await inspector.getByRole('link', { name: 'Download report', exact: true }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('report.md');
  await page.setViewportSize({ width: 430, height: 932 });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
  ).toBeTruthy();
  await page.screenshot({
    path: '../../../artifacts/management/workload-report.png',
    fullPage: true,
  });
});
