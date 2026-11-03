// A bounded live UI read test. No trace, video, HAR, storage-state export or secret output.
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { spawn } from "node:child_process";
import { performance } from "node:perf_hooks";

const root = fileURLToPath(new URL("../../", import.meta.url));
const require = createRequire(
  resolve(root, "apps/management/web/package.json"),
);
const { chromium, expect } = require("@playwright/test");
const config = JSON.parse(
  await readFile(resolve(root, "infra/terraform/project.json"), "utf8"),
);
const origin = `https://management.${config.domain}`;
const args = process.argv.slice(2);
if (
  args.length &&
  (args.length !== 2 ||
    args[0] !== "--restart-api" ||
    !/^[a-f0-9]{12}$/.test(args[1]))
) {
  throw new Error(
    "Use no arguments for read-only verification, or --restart-api <12-hex-operation>.",
  );
}
const output = resolve(root, "artifacts/platform/public-management-ui");
await mkdir(output, { recursive: true, mode: 0o700 });
let browser;
let context;
let session;
let stage = "launch";

async function restartProof() {
  const operation = args[1];
  const child = spawn(
    resolve(root, ".venv/bin/python"),
    [
      resolve(root, "scripts/infrastructure/management_api_failure.py"),
      "--operation",
      operation,
    ],
    { cwd: root, stdio: ["ignore", "pipe", "pipe"] },
  );
  let ended = false;
  let code;
  let stdout = "";
  child.stdout.on("data", (data) => {
    stdout += data.toString();
    if (stdout.length > 65536) child.kill();
  });
  child.stderr.on("data", () => {}); // Never forward potentially credential-bearing diagnostics.
  child.on("error", () => {
    code = -1;
    ended = true;
  });
  child.on("close", (status) => {
    code = status;
    ended = true;
  });
  const started = performance.now();
  const probes = [];
  let trailing = 0;
  try {
    while (performance.now() - started < 180000 && (!ended || trailing < 5)) {
      const sent = performance.now();
      const record = {
        sent_at: new Date().toISOString(),
        elapsed_seconds: (sent - started) / 1000,
      };
      try {
        const response = await context.request.get(
          origin + "/api/v1/auth/session",
          { timeout: 2000 },
        );
        record.status = response.status();
        await response.dispose();
      } catch {
        record.status = "transport_error";
      }
      record.duration_ms = performance.now() - sent;
      probes.push(record);
      if (ended) trailing += 1;
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    const failed = probes.filter((p) => p.status !== 200);
    const lastFailure = probes.findLastIndex((p) => p.status !== 200);
    const recovery = failed.length
      ? probes.slice(lastFailure + 1).find((p) => p.status === 200)
          ?.elapsed_seconds
      : 0;
    const result = {
      operation,
      probes,
      failed_probes: failed.length,
      recovery_upper_bound_seconds: recovery ?? null,
      timing:
        "Recovery upper bound starts before child preflight; probes are sequential at one-second intervals with a two-second timeout. Not inference/SLO measurements.",
      process_completed: ended && code === 0,
    };
    if (ended && code === 0) result.restart = JSON.parse(stdout);
    await writeFile(
      resolve(output, "api-failure-" + operation + ".json"),
      JSON.stringify(result, null, 2) + "\n",
      { mode: 0o600 },
    );
    if (!ended || code !== 0 || recovery === undefined || recovery > 60)
      throw new Error("API recovery");
    return result;
  } finally {
    if (!ended) child.kill("SIGTERM");
  }
}
try {
  browser = await chromium.launch({ headless: true });
  context = await browser.newContext({
    viewport: { width: 1600, height: 1050 },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  let errors = 0;
  page.on("pageerror", () => {
    errors += 1;
  });
  stage = "verified public HTTPS";
  const response = await page.goto(origin, { waitUntil: "domcontentloaded" });
  if (response.status() !== 200 || !(await response.securityDetails()))
    throw new Error("HTTPS");
  stage = "authentication";
  const password = (
    await readFile(resolve(root, "dev/.secrets/admin_password"), "utf8")
  ).trim();
  await page.getByLabel("Password", { exact: true }).fill(password);
  const login = page.waitForResponse(
    (r) =>
      r.url() === origin + "/api/v1/auth/login" &&
      r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Open workspace" }).click();
  const signedIn = await login;
  if (signedIn.status() !== 200) throw new Error("authentication");
  session = await signedIn.json();
  stage = "topology and inspector";
  await expect(
    page.getByRole("heading", { name: "Your platform, connected." }),
  ).toBeVisible();
  await expect(page.locator(".react-flow")).toHaveAttribute(
    "data-layout-ready",
    "true",
  );
  await page.getByRole("button", { name: "Fit View", exact: true }).click();
  await page.getByTestId("node-writer-assist").first().click();
  await expect(
    page.getByRole("complementary", { name: "Resource details" }),
  ).toContainText("Service contract");
  await page.screenshot({
    path: resolve(output, "topology.png"),
    fullPage: true,
  });
  stage = "retained runs";
  await page.getByRole("button", { name: "Close details" }).click();
  await page.getByRole("link", { name: "Runs & results" }).click();
  const records = await context.request.get(origin + "/api/v1/runs");
  if (records.status() !== 200) throw new Error("history");
  const runs = await records.json();
  await expect(
    page.getByRole("heading", { name: "Runs & results" }),
  ).toBeVisible();
  await page.screenshot({ path: resolve(output, "runs.png"), fullPage: true });
  if (errors) throw new Error("browser runtime");
  const cookies = await context.cookies();
  const authentication = cookies.find((c) => c.name === "management_session");
  if (!authentication?.secure || !authentication.httpOnly)
    throw new Error("cookie controls");
  let failure;
  if (args.length) {
    stage = "one API Pod restart under authenticated traffic";
    failure = await restartProof();
  }
  const result = {
    verified_at: new Date().toISOString(),
    origin,
    browser: browser.version(),
    public_certificate_verified: true,
    authenticated: true,
    browser_errors: errors,
    historical_runs: runs.length,
    cookie_secure: true,
    cookie_http_only: true,
    screenshots: ["topology.png", "runs.png"],
    scope: args.length
      ? "One exact API Pod restart under authenticated reads; no worker, database, host-loss or inference resilience claim"
      : "UI authentication, topology/inspector and retained-history reads; no resource mutations or inference claims",
    failure,
  };
  await writeFile(
    resolve(output, "verification.json"),
    JSON.stringify(result, null, 2) + "\n",
    { mode: 0o600 },
  );
  console.log(JSON.stringify(result));
} catch {
  // Playwright diagnostics can include entered text. Do not emit exception details.
  console.error(
    `Public management UI proof failed at: ${stage}. Details withheld.`,
  );
  process.exitCode = 1;
} finally {
  if (context && session) {
    try {
      const loggedOut = await context.request.post(
        origin + "/api/v1/auth/logout",
        {
          data: {},
          headers: { Origin: origin, "X-CSRF-Token": session.csrf_token },
        },
      );
      if (loggedOut.status() !== 204) throw new Error("logout");
    } catch {
      console.error("UI proof session cleanup failed; details withheld.");
      process.exitCode = 1;
    }
  }
  await context?.close();
  await browser?.close();
}
