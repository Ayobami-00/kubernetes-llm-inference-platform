// Opt-in proof against the owned HTTPS platform. Never records browser traces or auth values.
import { createHash } from "node:crypto";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { isDeepStrictEqual, parseArgs } from "node:util";

const root = fileURLToPath(new URL("../../", import.meta.url));
const { values } = parseArgs({
  options: {
    bundle: { type: "string" },
    output: { type: "string" },
    operation: { type: "string" },
  },
});
if (
  !values.bundle ||
  !values.output ||
  !/^[a-f0-9]{12}$/.test(values.operation || "")
) {
  throw new Error("Supply --bundle, --output and a twelve-hex --operation");
}
const output = path.resolve(values.output);
fs.mkdirSync(output, { recursive: true, mode: 0o700 });
const receipt = path.join(output, "ui-proof.json");
const content = fs.readFileSync(values.bundle);
const bundleSha = createHash("sha256").update(content).digest("hex");
const bundle = JSON.parse(content);
const snapshot = bundle.document.snapshot;
if (
  !bundle.document.fixture ||
  snapshot.target.mode !== "local-stub" ||
  !snapshot.admission_schedule.uri.startsWith(
    `file:///artifacts/acceptance/${values.operation}/`,
  )
) {
  throw new Error("This proof only accepts the exact staged synthetic cohort");
}
if (fs.existsSync(receipt)) {
  const previous = JSON.parse(fs.readFileSync(receipt));
  if (previous.bundle_sha256 !== bundleSha)
    throw new Error("Conflicting immutable UI inputs");
  if (previous.status === "verified") {
    console.log(
      JSON.stringify({
        status: "verified",
        run_id: previous.run_id,
        reused: true,
      }),
    );
    process.exit(0);
  }
  throw new Error(
    "Observe the original UI attempt before another launch; automatic replay disabled",
  );
}
const lock = path.join(output, "ui.lock");
fs.closeSync(fs.openSync(lock, "wx", 0o600));
let state = {
  operation: values.operation,
  bundle_sha256: bundleSha,
  status: "planned",
};
const write = () => {
  const temporary = receipt + ".tmp";
  fs.writeFileSync(temporary, JSON.stringify(state, null, 2) + "\n", {
    mode: 0o600,
  });
  fs.renameSync(temporary, receipt);
};
write();
let browser;
let page;
let authenticated = false;
try {
  const require = createRequire(
    path.join(root, "apps/management/web/package.json"),
  );
  const { chromium } = require("playwright");
  const project = JSON.parse(
    fs.readFileSync(path.join(root, "infra/terraform/project.json")),
  );
  const origin = `https://management.${project.domain}`;
  browser = await chromium.launch({ headless: true });
  // No storageState, trace, video or network capture is retained.
  const context = await browser.newContext({
    viewport: { width: 1600, height: 1050 },
  });
  page = await context.newPage();
  page.setDefaultTimeout(30000);
  await page.goto(origin);
  if (new URL(page.url()).origin !== origin)
    throw new Error("Unexpected authentication origin");
  const password = fs
    .readFileSync(path.join(root, "dev/.secrets/admin_password"), "utf8")
    .trim();
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page
    .getByRole("button", { name: "Open workspace", exact: true })
    .click();
  await page.getByRole("link", { name: "Imports", exact: true }).click();
  authenticated = true;
  const identity = `browser-workload-${values.operation}`;
  await page.getByLabel("Document type").selectOption("prepared-runs");
  await page
    .getByLabel("Source name")
    .fill(`AWS UI workload proof ${values.operation}`);
  await page.getByLabel("Source provenance").selectOption("synthetic");
  await page.getByLabel("Import JSON file").setInputFiles({
    name: "synthetic-prepared-run.json",
    mimeType: "application/json",
    buffer: Buffer.from(
      JSON.stringify({ id: identity, fixture: true, snapshot }),
    ),
  });
  state.status = "importing";
  write();
  await page
    .getByRole("button", { name: "Confirm import", exact: true })
    .click();
  await page
    .locator(".import-receipt")
    .filter({ hasText: identity })
    .first()
    .getByText("completed", { exact: true })
    .waitFor({ timeout: 60000 });
  await page.getByRole("link", { name: "Simulation lab", exact: true }).click();
  await page
    .getByRole("button")
    .filter({ hasText: "writer-assist-baseline" })
    .first()
    .click();
  const inspector = page.getByRole("complementary", {
    name: "Resource details",
  });
  await inspector.getByRole("tab", { name: "overview", exact: true }).click();
  await inspector.getByLabel("Prepared run bundle").selectOption(identity);
  await inspector
    .getByRole("button", { name: "Check readiness", exact: true })
    .click();
  const launch = inspector.getByRole("button", {
    name: "Run local workload",
    exact: true,
  });
  await launch.waitFor();
  const accepted = page.waitForResponse(
    (r) =>
      r.url() === origin + "/api/v1/runs" && r.request().method() === "POST",
  );
  state.status = "launching";
  write();
  await launch.click();
  const response = await accepted;
  if (response.status() !== 202) throw new Error("Launch rejected");
  const action = await response.json();
  state = {
    ...state,
    status: "launched",
    run_id: action.resource_id,
    operation_id: action.operation_id,
  };
  write();
  await page
    .getByRole("button", { name: "Close details", exact: true })
    .click();
  await page.getByRole("link", { name: "Runs & results", exact: true }).click();
  const row = page
    .getByRole("button")
    .filter({ hasText: state.run_id })
    .first();
  const deadline = Date.now() + 240000;
  while (!(await row.textContent()).includes("completed")) {
    if (Date.now() > deadline)
      throw new Error("Original UI attempt did not complete");
    await page.waitForTimeout(1000);
  }
  await row.click();
  const downloadEvent = page.waitForEvent("download");
  await inspector
    .getByRole("link", { name: "Download report", exact: true })
    .click();
  const download = await downloadEvent;
  await download.saveAs(path.join(output, "report.md"));
  const viewResponse = await context.request.get(
    origin + "/api/v1/runs/" + state.run_id,
  );
  if (!viewResponse.ok()) throw new Error("Observation failed");
  const view = await viewResponse.json();
  const report = fs.readFileSync(path.join(output, "report.md"));
  if (
    !isDeepStrictEqual(view.run.snapshot, snapshot) ||
    !view.run.fixture ||
    createHash("sha256").update(report).digest("hex") !==
      view.run.result.artifacts.report.sha256
  ) {
    throw new Error("Frozen snapshot or downloaded report differs");
  }
  state = {
    ...state,
    fixture: true,
    execution: view.run.status,
    validity: view.run.result.validity,
    slo_verdict: view.run.result.slo_verdict,
    timeline_origin_at: view.timeline_origin_at,
    report_sha256: view.run.result.artifacts.report.sha256,
  };
  write();
  await page.screenshot({
    path: path.join(output, "run-inspector.png"),
    fullPage: true,
  });
  if (view.run.result.validity !== "valid" || !view.timeline_origin_at) {
    throw new Error("New run lacks valid delivery or its original timeline");
  }
  state.status = "verified";
  state.verified_at = new Date().toISOString();
  write();
  console.log(JSON.stringify(state));
} catch (error) {
  // Playwright action diagnostics may contain fill() arguments: never print them.
  state = {
    ...state,
    status: "failed",
    failed_at: new Date().toISOString(),
    error_type: error.name,
  };
  write();
  console.error(
    JSON.stringify({
      status: "failed",
      run_id: state.run_id,
      error_type: error.name,
      detail:
        "Inspect the private receipt and original operation; credentials withheld",
    }),
  );
  process.exitCode = 1;
} finally {
  if (authenticated && page) {
    const logout = page.getByRole("button", { name: "Sign out", exact: true });
    try {
      if (await logout.count()) await logout.click();
    } catch {
      /* context closes below */
    }
  }
  await browser?.close();
  fs.unlinkSync(lock);
}
