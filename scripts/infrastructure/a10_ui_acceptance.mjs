// Opt-in real-console acceptance. Ordinary Playwright CI never invokes this file.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const require = createRequire(resolve(root, 'apps/management/web/package.json'));
const { chromium, expect } = require('@playwright/test');
const phase = process.argv[2] || 'inspect';
const identity = process.argv[3] || '';
const directory = process.argv[4];
const waitSeconds = Number(process.argv[5] || 0);
if (!['inspect', 'publish-profile', 'provision'].includes(phase) || !directory ||
    !Number.isInteger(waitSeconds) || waitSeconds < 0 || waitSeconds > 900 ||
    (phase === 'provision' && !/^writer-a10-[a-z0-9-]{1,40}$/.test(identity))) {
  throw new Error('Use inspect|publish-profile|provision <identity> <private-output-directory>');
}
mkdirSync(directory, { recursive: true, mode: 0o700 });
let stage = 'startup';
let browser;
let page;
const save = (name, value) => writeFileSync(resolve(directory, name),
  JSON.stringify(value, null, 2) + '\n', { mode: 0o600, flag: 'wx' });

async function main() {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    baseURL: 'https://management.klip-lab.click', viewport: { width: 1600, height: 1050 },
  });
  page = await context.newPage();
  page.setDefaultTimeout(30000);
  stage = 'authentication';
  stage = 'login-navigation';
  await page.goto('/');
  const password = readFileSync(resolve(root, 'dev/.secrets/admin_password'), 'utf8').trim();
  stage = 'login-fill';
  await page.getByLabel('Password', { exact: true }).fill(password);
  stage = 'login-submit';
  await page.getByRole('button', { name: 'Open workspace', exact: true }).click();
  stage = 'workspace-ready';
  await page.getByRole('heading', { name: 'Your platform, connected.', exact: true }).waitFor();
  stage = 'read-worker-specifications';
  const specsResponse = await page.request.get('/api/v1/worker-group-specs');
  if (!specsResponse.ok()) {
    save('specification-read-status.json', { http_status: specsResponse.status() });
    throw new Error('Specification read failed');
  }
  let current = (await specsResponse.json()).find(s => s.id === 'latency-workers');
  if (!current) throw new Error('Expected seeded worker specification');
  const matches = value => value?.instance_type === 'gpu_1x_a10' &&
    value?.region === 'us-east-1' && value?.desired_instances === 1 &&
    value?.hardware?.gpu_model === 'A10' && value?.hardware?.gpu_count === 1 &&
    value?.ttl_minutes === 90 && value?.drain_timeout_s === 120;
  if (phase === 'publish-profile') {
    stage = 'publish-worker-profile';
    if (current.current_revision === 1 && !current.draft) {
      stage = 'profile-fit-graph';
      await page.getByRole('button', { name: 'Fit View', exact: true }).click();
      stage = 'profile-select-worker';
      await page.getByTestId('node-latency-workers').click();
      stage = 'profile-open-editor';
      await page.getByRole('tab', { name: 'configure', exact: true }).click();
      await page.getByLabel('Lambda instance type', { exact: true }).fill('gpu_1x_a10');
      await page.getByLabel('Region', { exact: true }).fill('us-east-1');
      stage = 'profile-notes';
      await page.getByLabel(/^Notes/).fill(
        'Verified single-A10 SKU and Virginia region. Derived from latency-workers@1; ' +
        'advertised hardware only, actual GPU/network/CUDA readiness remains required.');
      stage = 'profile-save-draft';
      await page.getByRole('button', { name: 'Save draft', exact: true }).click();
      stage = 'profile-wait-draft';
      await page.getByText('Draft saved · ready to publish', { exact: true }).waitFor();
      stage = 'profile-publish';
      const publication = page.waitForResponse(r =>
        r.url().endsWith('/api/v1/worker-group-specs/latency-workers/publish') &&
        r.request().method() === 'POST');
      await page.getByRole('button', { name: 'Publish revision', exact: true }).click();
      stage = 'profile-wait-published';
      const published = await publication;
      if (published.status() !== 200) throw new Error('Profile publication rejected');
      await page.getByText('latency-workers@2', { exact: true }).waitFor();
      current = (await (await page.request.get('/api/v1/worker-group-specs')).json())
        .find(s => s.id === 'latency-workers');
    }
    if (current.current_revision !== 2 || current.draft || !matches(current.document))
      throw new Error('Existing revision/draft requires explicit reconciliation');
    save('published-profile.json', current);
    await page.screenshot({ path: resolve(directory, 'published-profile.png'), fullPage: true });
  }
  stage = 'inspect-capacity';
  await page.getByRole('link', { name: 'GPU capacity', exact: true }).click();
  const form = page.getByRole('region', { name: 'Provision live Lambda worker', exact: true });
  await form.waitFor();
  const capacityDeadline = Date.now() + waitSeconds * 1000;
  let capacity, options, offering;
  const observations = [];
  do {
    const before = await (await page.request.get('/api/v1/capacity')).json();
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    const refreshDeadline = Date.now() + 45000;
    do {
      const response = await page.request.get('/api/v1/capacity');
      if (!response.ok()) throw new Error('Capacity observation failed');
      capacity = await response.json();
      const age = Date.now() - Date.parse(capacity.observed_at);
      if (capacity.status === 'fresh' && capacity.observed_at !== before.observed_at &&
          Number.isFinite(age) && age >= -5000 && age < 15000) break;
      await new Promise(r => setTimeout(r, 1000));
    } while (Date.now() < refreshDeadline);
    options = await (await page.request.get('/api/v1/worker-lifecycle-options')).json();
    offering = capacity?.instance_types?.find(s => s.name === 'gpu_1x_a10');
    const available = Boolean(offering?.regions_with_capacity_available?.some(r => r.name === 'us-east-1'));
    observations.push({ observed_at: capacity?.observed_at, status: capacity?.status, a10_available: available });
    if (phase !== 'provision' || available || Date.now() >= capacityDeadline) break;
    if (!options.configured || options.region !== 'us-east-1' ||
        options.instance_type !== 'gpu_1x_a10' || offering?.price_cents_per_hour !== 129)
      throw new Error('Policy or price changed while waiting; no allocation submitted');
    stage = 'waiting-approved-capacity';
    await new Promise(r => setTimeout(r, Math.min(60000, capacityDeadline - Date.now())));
  } while (Date.now() <= capacityDeadline);
  const summary = { observed_at: capacity?.observed_at, status: capacity?.status,
    lifecycle_configured: options.configured, compatible_specs: options.compatible_specs,
    a10: offering, phase, checked_at: new Date().toISOString() };
  save('capacity.json', summary);
  save('capacity-observations.json', observations);
  await page.screenshot({ path: resolve(directory, 'capacity.png'), fullPage: true });
  if (phase === 'provision') {
    stage = 'bounded-provision';
    if (!matches(current.document) || current.current_revision !== 2 ||
        options.instance_type !== 'gpu_1x_a10' || options.region !== 'us-east-1' ||
        options.price_ceiling_cents_per_hour !== 129 || !options.configured ||
        capacity?.status !== 'fresh' || Date.now() - Date.parse(capacity.observed_at) >= 60000 ||
        !offering || offering.price_cents_per_hour !== 129 ||
        !offering.regions_with_capacity_available.some(r => r.name === 'us-east-1'))
      throw new Error('Approved A10 capacity/profile is unavailable; no allocation submitted');
    const intent = resolve(directory, 'provision-intent.json');
    if (existsSync(intent)) throw new Error('Original submission needs observation, never repeat it');
    stage = 'provision-select-profile';
    await form.getByLabel(/^Worker profile & lease/).selectOption('latency-workers@2');
    stage = 'provision-name';
    await form.getByLabel('Group name', { exact: true }).fill(identity);
    const submit = form.getByRole('button', { name: 'Provision worker', exact: true });
    save('provision-form.json', { text: await form.innerText() });
    await expect(submit).toBeEnabled({ timeout: 15000 });
    save('provision-intent.json', { identity, spec_ref: 'latency-workers@2',
      mode: 'real', initial_lease_minutes: 90, max_price_cents_per_hour: 129,
      submitted_at: new Date().toISOString() });
    const responsePromise = page.waitForResponse(r =>
      r.url().endsWith('/api/v1/worker-groups') && r.request().method() === 'POST');
    stage = 'provision-submit';
    await form.getByRole('button', { name: 'Provision worker', exact: true }).click();
    const response = await responsePromise;
    const result = await response.json();
    save('provision-response.json', { http_status: response.status(), result });
    if (response.status() !== 202 || result.resource_id !== identity)
      throw new Error('Provisioning was rejected; retain original intent and response');
    await page.getByRole('link', { name: 'Operations', exact: false }).click();
    await page.locator('.operation-card').filter({ hasText: identity }).waitFor();
    await page.screenshot({ path: resolve(directory, 'provisioned-operation.png'), fullPage: true });
    console.log(JSON.stringify({ status: 'accepted', ...result, phase }));
  } else console.log(JSON.stringify({ status: 'verified', phase, lifecycle_configured: options.configured,
    compatible_specs: options.compatible_specs, a10_available:
      Boolean(offering?.regions_with_capacity_available?.some(r => r.name === 'us-east-1')) }));
}

try { await main(); }
catch (error) {
  // Playwright errors can embed fill arguments. Never print their message/stack.
  console.error(JSON.stringify({ status: 'failed', stage, error_type: error.name }));
  if (stage.startsWith('profile-')) {
    await page.screenshot({ path: resolve(directory, 'profile-diagnostic.png'), fullPage: true });
    save('profile-diagnostic.json', {
      buttons: await page.getByRole('button').allTextContents(),
      node_ids: await page.locator('[data-testid^="node-"]').evaluateAll(
        elements => elements.map(element => element.getAttribute('data-testid'))),
    });
  }
  process.exitCode = 1;
} finally { await browser?.close(); }
