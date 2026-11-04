// Opt-in actual-dashboard acceptance. No provider, host or arbitrary model actions.
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const require = createRequire(resolve(root, 'apps/management/web/package.json'));
const { chromium, expect } = require('@playwright/test');
const [phase, output, identity = ''] = process.argv.slice(2);
if (!['inspect', 'launch', 'cancel', 'collect'].includes(phase) || !output ||
    (['cancel', 'collect'].includes(phase) && !/^experiment-[a-f0-9]{20}$/.test(identity))) {
  throw new Error('Use inspect|launch|cancel|collect <private-output-directory> [experiment-id]');
}
const directory = resolve(output);
if (!directory.startsWith(resolve(root, 'artifacts') + '/')) throw new Error('Use ignored artifact storage');
mkdirSync(directory, { recursive: true, mode: 0o700 });
const save = (name, data) => writeFileSync(resolve(directory, name),
  JSON.stringify(data, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
if (phase === 'launch' && existsSync(resolve(directory, 'launch-intent.json')))
  throw new Error('Observe the saved launch; never submit a replacement automatically');

let stage = 'startup';
let browser;
try {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ baseURL: 'https://management.klip-lab.click',
    viewport: { width: 1600, height: 1100 } });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  stage = 'login';
  await page.goto('/');
  await page.getByLabel('Password', { exact: true }).fill(
    readFileSync(resolve(root, 'dev/.secrets/admin_password'), 'utf8').trim());
  await page.getByRole('button', { name: 'Open workspace', exact: true }).click();
  await page.getByRole('heading', { name: 'Your platform, connected.', exact: true }).waitFor();
  stage = 'experiments-navigation';
  await page.getByRole('link', { name: 'Experiments', exact: true }).click();
  await page.getByRole('heading', { name: 'WriterAssist · baseline vs n-gram', exact: true }).waitFor();

  if (phase === 'launch' || phase === 'inspect') {
    stage = 'preview';
    const response = await page.request.get('/api/v1/experiments/a10/preview');
    if (!response.ok()) throw new Error('Preview request failed');
    const preview = await response.json();
    save('preview.json', preview);
    if (phase === 'launch') {
      if (!preview.available || preview.summary.node !== 'writer-a10-retained' ||
          preview.summary.model?.repository !== 'Qwen/Qwen3-4B' ||
          preview.summary.speculation?.method !== 'ngram')
        throw new Error('The fixed reviewed A10 preset is unavailable; no launch submitted');
      stage = 'review';
      await page.getByText('Review frozen experiment settings', { exact: true }).click();
      const button = page.getByRole('button', { name: 'Run A10 baseline vs n-gram', exact: true });
      await expect(button).toBeEnabled();
      await page.screenshot({ path: resolve(directory, 'review.png'), fullPage: true });
      save('launch-intent.json', { definition_sha256: preview.definition_sha256,
        clicked_at: new Date().toISOString(), surface: 'actual-management-dashboard' });
      const accepted = page.waitForResponse(r => r.url().endsWith('/api/v1/experiments/a10') &&
        r.request().method() === 'POST');
      stage = 'launch-click';
      await button.click();
      const result = await accepted;
      const body = await result.json();
      save('launch-response.json', { http_status: result.status(), ...body });
      if (result.status() !== 202) throw new Error('Dashboard launch was rejected; retain intent');
      const card = page.locator('.experiment-card').filter({ hasText: body.resource_id });
      await card.waitFor();
      await card.screenshot({ path: resolve(directory, 'launched.png') });
      console.log(JSON.stringify({ status: 'accepted', ...body }));
    } else {
      await page.screenshot({ path: resolve(directory, 'preview.png'), fullPage: true });
      console.log(JSON.stringify({ status: 'inspected', available: preview.available,
        blockers: preview.blockers }));
    }
  } else {
    stage = 'observe-parent';
    const response = await page.request.get('/api/v1/experiments/' + identity);
    if (!response.ok()) throw new Error('Experiment observation failed');
    const observed = await response.json();
    save('experiment.json', observed);
    const card = page.locator('.experiment-card').filter({ hasText: identity });
    await card.waitFor();
    if (phase === 'cancel' && !['completed', 'failed', 'cancelled'].includes(observed.status)) {
      stage = 'cancel-click';
      const result = page.waitForResponse(r => r.url().endsWith('/api/v1/experiments/' + identity + '/stop') &&
        r.request().method() === 'POST');
      await card.getByRole('button', { name: 'Cancel experiment', exact: true }).click();
      const accepted = await result;
      save('cancel-response.json', { http_status: accepted.status(), result: await accepted.json() });
      if (accepted.status() !== 202) throw new Error('Dashboard cancellation was rejected');
    }
    if (phase === 'collect' && ['completed', 'failed', 'cancelled'].includes(observed.status)) {
      stage = 'verified-downloads';
      const verified = {};
      for (const name of ['report', 'comparison', 'overlap', 'disagreements', 'definition', 'execution']) {
        const artifact = observed.artifacts?.[name];
        if (!artifact) continue;
        const download = await page.request.get('/api/v1/artifacts/' + encodeURIComponent(artifact.id));
        if (!download.ok()) throw new Error('Registered artifact download failed');
        const bytes = await download.body();
        if (bytes.length > 32 * 1024 * 1024 ||
            createHash('sha256').update(bytes).digest('hex') !== artifact.sha256)
          throw new Error('Downloaded artifact checksum/size differs');
        const filename = name === 'report' ? 'report.md' : name + '.json';
        writeFileSync(resolve(directory, filename), bytes, { mode: 0o600, flag: 'wx' });
        verified[name] = { sha256: artifact.sha256, bytes: bytes.length };
      }
      save('verified-downloads.json', verified);
    }
    await card.screenshot({ path: resolve(directory, 'experiment.png') });
    console.log(JSON.stringify({ status: observed.status, experiment_id: identity,
      phase, stage: observed.stage, children: observed.children }));
  }
} catch (error) {
  // Login/Playwright errors may include fill arguments. Never print their text.
  console.error(JSON.stringify({ status: 'failed', stage, error_type: error.name }));
  process.exitCode = 1;
} finally {
  await browser?.close();
}
