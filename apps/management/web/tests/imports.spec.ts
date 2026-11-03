import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import AxeBuilder from '@axe-core/playwright';

const password = () =>
  process.env.MANAGEMENT_E2E_PASSWORD ||
  readFileSync(
    process.env.MANAGEMENT_E2E_PASSWORD_FILE || resolve('../../../dev/.secrets/admin_password'),
    'utf8',
  ).trim();
test('reviewed historical imports persist and preserve their source in the frozen graph', async ({
  page,
}) => {
  await page.goto('/');
  await page.getByLabel('Password', { exact: true }).fill(password());
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await page.getByRole('link', { name: 'Imports', exact: true }).click();
  const document = JSON.parse(
    readFileSync(
      resolve('../../../contracts/fixtures/runs/writer-assist/completed-violating.json'),
      'utf8',
    ),
  );
  document.id = 'browser-import-' + Date.now();
  await page.getByLabel('Document type').selectOption('runs');
  await page.getByLabel('Source name').fill('Browser fixture import');
  await page.getByLabel('Source provenance').selectOption('synthetic');
  await page.getByLabel('Import JSON file').setInputFiles({
    name: 'synthetic-result.json',
    mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify(document)),
  });
  await expect(page.getByText(document.id, { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Confirm import', exact: true }).click();
  const receipt = page.locator('.import-receipt').filter({ hasText: 'Browser fixture import' });
  await expect(receipt.first()).toContainText('completed', { timeout: 30000 });
  await page.reload();
  await expect(receipt.first()).toContainText(document.id);
  await page.getByRole('link', { name: 'Runs & results', exact: true }).click();
  await page.getByRole('button').filter({ hasText: document.id }).click();
  const inspector = page.getByRole('complementary', { name: 'Resource details' });
  await expect(inspector).toContainText('Browser fixture import');
  await expect(inspector).toContainText('Synthetic fixture data');
  await expect(inspector).toContainText('violating');
  await page.getByRole('button', { name: 'Explore frozen snapshot' }).click();
  await expect(page.getByRole('heading', { name: 'Frozen run snapshot' })).toBeVisible();
  await page.getByRole('link', { name: 'Integrations', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Platform readiness' })).toBeVisible();
  await expect(page.locator('.summary-grid')).toHaveCount(0);
  for (const theme of ['light', 'dark']) {
    await page.getByRole('button', { name: 'Use ' + theme + ' theme' }).click();
    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
      .analyze();
    expect(
      results.violations.map((v) => ({ id: v.id, nodes: v.nodes.map((n) => n.failureSummary) })),
    ).toEqual([]);
  }
  await page.screenshot({
    path: '../../../artifacts/management/integration-readiness.png',
    fullPage: true,
  });
  await page.setViewportSize({ width: 430, height: 932 });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
  ).toBeTruthy();
  await page.getByRole('link', { name: 'Imports', exact: true }).click();
  await expect(receipt.first()).toContainText('completed');
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
  ).toBeTruthy();
});
