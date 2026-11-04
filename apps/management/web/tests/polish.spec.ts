import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
const password = () =>
  process.env.MANAGEMENT_E2E_PASSWORD ||
  readFileSync(
    process.env.MANAGEMENT_E2E_PASSWORD_FILE || resolve('../../../dev/.secrets/admin_password'),
    'utf8',
  ).trim();
async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Password', { exact: true }).fill(password());
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await expect(page.getByRole('heading', { name: 'Your platform, connected.' })).toBeVisible();
  await expect(page.locator('.react-flow')).toHaveAttribute('data-layout-ready', 'true');
}
async function inspect(page: Page, id: string) {
  const panel = page.getByRole('complementary', { name: 'Resource details' });
  if (await panel.isVisible()) await page.getByRole('button', { name: 'Close details' }).click();
  await page.getByTestId(`node-${id}`).first().waitFor({ state: 'attached' });
  await page.getByRole('button', { name: 'Fit View', exact: true }).click();
  await page.getByTestId(`node-${id}`).first().click();
  await expect(panel).toBeVisible();
  return panel;
}
test('visible resources retain their measurements when layers change repeatedly', async ({
  page,
}) => {
  await page.goto('/');
  await signIn(page);
  await page.getByRole('button', { name: 'Layers', exact: true }).click();
  const scenarios = page.getByLabel('Scenario', { exact: true });
  for (let change = 0; change < 5; change++) {
    await scenarios.check();
    await expect(page.getByTestId('node-platform-mixed').first()).toBeVisible();
    await scenarios.uncheck();
    await expect(page.getByTestId('node-platform-mixed')).toHaveCount(0);
    await expect(page.getByTestId('node-writer-assist').first()).toBeVisible();
  }
});

for (const theme of ['dark', 'light'] as const) {
  test(`${theme} appearance persists and specifications are readable`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.goto('/');
    await page.getByRole('button', { name: `Use ${theme} theme` }).click();
    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
    await signIn(page);
    await expect(page.locator('.react-flow')).toHaveClass(new RegExp(theme));
    await expect(page.getByTestId('node-writer-assist').first()).toBeVisible();
    const before = await page.locator('.react-flow__viewport').getAttribute('style');
    await page
      .getByRole('button', { name: `Use ${theme === 'dark' ? 'light' : 'dark'} theme` })
      .click();
    await page.getByRole('button', { name: `Use ${theme} theme` }).click();
    await expect(page.locator('.react-flow__viewport')).toHaveAttribute('style', before!);
    let panel = await inspect(page, 'writer-assist');
    await expect(panel.getByRole('region', { name: 'Configuration details' })).toContainText(
      'Rendered input ceiling',
    );
    await expect(panel).toContainText('Quality evaluation intent');
    await expect(panel.locator('pre')).toHaveCount(0);
    await panel.getByRole('tab', { name: 'history' }).click();
    await panel.getByText('Review this revision', { exact: true }).first().click();
    await expect(panel).toContainText('SLO policy');
    await expect(panel.locator('pre')).toHaveCount(0);
    panel = await inspect(page, 'latency-workers');
    await expect(panel).toContainText('Lease & cleanup');
    await expect(panel).toContainText('Not configured');
    panel = await inspect(page, 'gec-baseline');
    await expect(panel).toContainText('Deployment topology');
    await expect(panel).toContainText('80%');
    panel = await inspect(page, 'jfleg-test');
    await expect(panel).toContainText('Preparation pending');
    await expect(panel).toContainText('Not reviewed');
    await expect(panel.locator('pre')).toHaveCount(0);
    await page.getByRole('button', { name: 'Close details' }).click();
    await page.getByRole('button', { name: 'Layers', exact: true }).click();
    await page.getByLabel('Scenario', { exact: true }).check();
    panel = await inspect(page, 'platform-mixed');
    await expect(panel.getByLabel('Traffic phase durations')).toBeVisible();
    await expect(panel).toContainText('Measurement duration');
    await page.getByRole('button', { name: 'Close details' }).click();
    await page.getByLabel('Scenario', { exact: true }).uncheck();
    await page.getByRole('button', { name: 'Layers', exact: true }).click();
    await inspect(page, 'writer-assist');
    expect(
      await page.evaluate(() => ({
        x: document.documentElement.scrollWidth <= innerWidth,
        y: document.documentElement.scrollHeight <= innerHeight,
      })),
    ).toEqual({ x: true, y: true });
    await page.screenshot({
      path: `../../../artifacts/management/${theme}-inspector.png`,
      fullPage: true,
    });
    expect(errors).toEqual([]);
  });
  test(`${theme} login and workspace views meet automated accessibility checks`, async ({
    page,
  }) => {
    await page.goto('/');
    await page.getByRole('button', { name: `Use ${theme} theme` }).click();
    const login = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
      .analyze();
    expect(login.violations).toEqual([]);
    await signIn(page);
    await inspect(page, 'writer-assist');
    const workspace = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
      .analyze();
    expect(workspace.violations).toEqual([]);
    await page.getByRole('button', { name: 'Close details' }).click();
    for (const name of [
      'Customers',
      'GPU capacity',
      'Runs & results',
      'Operations',
      'Simulation lab',
      'Imports',
      'Integrations',
    ]) {
      await page.getByRole('link', { name, exact: true }).click();
      const view = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
        .analyze();
      expect(
        view.violations.map((violation) => ({
          id: violation.id,
          targets: violation.nodes.map((node) => node.target),
          details: violation.nodes.map((node) => node.failureSummary),
        })),
        `Accessibility violations on ${name} in ${theme} mode`,
      ).toEqual([]);
    }
  });
  test(`${theme} mobile inspector, themes and keyboard close fit the viewport`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 430, height: 932 });
    await signIn(page);
    await page.getByRole('button', { name: `Use ${theme} theme` }).click();
    await page.getByRole('link', { name: 'Customers', exact: true }).click();
    const customer = page.getByRole('button').filter({ hasText: 'WriterAssist' }).first();
    await customer.focus();
    await customer.press('Enter');
    const panel = page.getByRole('complementary', { name: 'Resource details' });
    await expect(panel).toContainText('Request limits');
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    ).toBeTruthy();
    await page.screenshot({
      path: `../../../artifacts/management/${theme}-mobile.png`,
      fullPage: true,
    });
    await page.keyboard.press('Escape');
    await expect(panel).toHaveCount(0);
    await expect(customer).toBeFocused();
  });
}
test('system appearance follows OS changes and explicit choices override it', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/');
  await page.getByRole('button', { name: 'Use system theme' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.getByRole('button', { name: 'Use light theme' }).click();
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.reload();
  await expect(page.getByRole('button', { name: 'Use light theme' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
});
test('advanced editing preserves changes, blocks stale publication and completes new customer creation', async ({
  page,
}) => {
  await signIn(page);
  await page.getByRole('button', { name: 'Create', exact: true }).click();
  await page.getByRole('button', { name: 'Customer', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Publish revision', exact: true })).toBeDisabled();
  const id = `ui-editor-${Date.now()}`;
  await page.getByLabel('Stable ID', { exact: true }).fill(id);
  await page.getByLabel('Customer name', { exact: true }).fill('UI editor regression');
  await page.getByRole('button', { name: 'Advanced JSON', exact: true }).click();
  const editor = page.getByRole('textbox', { name: 'Advanced configuration', exact: true });
  const document = JSON.parse(await editor.inputValue());
  document.generation.max_output_tokens += 1;
  await editor.fill(JSON.stringify(document, null, 2));
  await page.getByRole('button', { name: 'Form editor', exact: true }).click();
  await expect(page.getByLabel('Output token limit', { exact: true })).toHaveValue(
    String(document.generation.max_output_tokens),
  );
  await page.getByRole('button', { name: 'Save draft', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('Draft saved');
  await page
    .getByLabel('Output token limit', { exact: true })
    .fill(String(document.generation.max_output_tokens + 1));
  await expect(page.getByRole('button', { name: 'Publish revision', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Advanced JSON', exact: true }).click();
  const correct = await editor.inputValue();
  await editor.fill('{invalid JSON');
  await page.getByRole('button', { name: 'Form editor', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Fix the JSON syntax');
  await expect(editor).toBeVisible();
  await editor.fill(correct);
  await page.getByRole('button', { name: 'Form editor', exact: true }).click();
  await page.getByRole('button', { name: 'Save draft', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('Draft saved');
  await page.getByRole('button', { name: 'Publish revision', exact: true }).click();
  await expect(page.getByRole('complementary', { name: 'Resource details' })).toContainText(
    `${id}@1`,
  );
  await expect(
    page.getByRole('heading', { name: 'UI editor regression', exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel('Stable ID', { exact: true })).toHaveCount(0);
});
