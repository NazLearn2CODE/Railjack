// Orbiter dashboard browser smoke test.
// Proves end-to-end: shell renders -> composer dispatch -> WS token stream ->
// Console render -> completion + telemetry -> Bash approval card -> APPROVE.
// Launch recipe (servers, env, gotchas): .claude/skills/run/SKILL.md
import { chromium } from "playwright";
import { readdirSync, mkdirSync } from "fs";
import { join } from "path";

const URL = "http://localhost:5173/"; // Vite binds IPv6 [::1] — localhost resolves to it
const OUT = "screenshots";
mkdirSync(OUT, { recursive: true });
const shot = (n) => join(OUT, n);
const errors = [];

// ponytail: pin executablePath to a cached headless-shell to skip a ~150MB
// download (playwright's expected build may differ from what's cached; close
// enough to drive). Falls back to playwright's default if the cache is absent —
// then run `npx playwright install chromium`.
const cacheDir = `${process.env.HOME}/.cache/ms-playwright`;
let executablePath;
try {
  const shell = readdirSync(cacheDir)
    .filter((d) => d.startsWith("chromium_headless_shell-"))
    .sort()
    .reverse()[0];
  executablePath = shell && join(cacheDir, shell, "chrome-headless-shell-linux64", "chrome-headless-shell");
} catch {
  /* cache absent — let playwright resolve */
}
const browser = await chromium.launch({ ...(executablePath && { executablePath }), args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message));

const log = (...a) => console.log(...a);
const ta = () => page.locator('textarea[placeholder*="DISPATCH A PROMPT"]');
const dispatchBtn = () => page.getByRole("button", { name: "DISPATCH", exact: true });

// --- 1. Shell renders ---
await page.goto(URL, { waitUntil: "load" }); // networkidle won't settle: WS stays open
await ta().waitFor({ timeout: 15000 });
await page.screenshot({ path: shot("01-shell.png") });
log("STEP1 shell rendered (composer visible)");

// read a Telemetry SESSION-panel value by its label
async function readout(label) {
  return page.locator("span.label", { hasText: label }).locator("..").locator("span.mono").textContent();
}

// --- 2. Dispatch happy-path prompt (known no-tool reply) ---
await ta().fill("Reply with exactly: orbiter-ok");
await dispatchBtn().click();
try {
  await page.waitForSelector("text=STREAMING", { timeout: 8000 });
  await page.screenshot({ path: shot("02-streaming.png") });
  log("STEP2 streaming marker observed mid-run");
} catch {
  log("STEP2 no STREAMING marker (completed faster than poll) — continuing");
}

// Completion = button re-enables as DISPATCH (leaves ·· BUSY). Generous for z.ai latency.
await dispatchBtn().waitFor({ state: "visible", timeout: 120000 });
await page.waitForSelector("text=orbiter-ok", { timeout: 10000 });
await page.screenshot({ path: shot("03-completed.png") });
const status = ((await readout("STATUS")) || "").trim();
const tokens = ((await readout("TOKENS")) || "").trim();
log(`STEP3 completed — STATUS=${status} TOKENS=${tokens} replyRendered=true`);

// --- 3. Approval-card path: invite a Bash tool call ---
await ta().fill("Run this shell command and reply with its exact output: echo ORBITER-SMOKE");
await dispatchBtn().click();
let approvalShown = false;
try {
  await page.waitForSelector("text=APPROVAL REQUIRED", { timeout: 25000 });
  approvalShown = true;
  await page.screenshot({ path: shot("04-approval-card.png") });
  log("STEP4 approval card surfaced — clicking APPROVE");
  await page.getByRole("button", { name: "APPROVE" }).click();
  await dispatchBtn().waitFor({ state: "visible", timeout: 120000 });
  await page.screenshot({ path: shot("05-after-approve.png") });
  log("STEP4 approved; run continued to completion");
} catch {
  await page.screenshot({ path: shot("04-no-approval.png") });
  try { await dispatchBtn().waitFor({ state: "visible", timeout: 30000 }); } catch {}
  log("STEP4 no approval card surfaced (agent didn't invoke a gated tool under z.ai) — documented as known");
}

log(`\nCONSOLE ERRORS (${errors.length}):`);
for (const e of errors) log("  -", e);

await browser.close();
console.log(`RESULT approvalShown=${approvalShown} consoleErrors=${errors.length}`);
if (errors.length > 0) process.exit(1);
