#!/usr/bin/env node
// Drives the phone page in WebKit as an iPhone, against a copy of the server run with --dev on its own port
// and its own state folder.
//
// It runs in your real cmux, as cmux has no private server to test against. It works only in a workspace it
// makes itself, named "cmux-phone test", and closes it at the end. It never opens or types into your panes.
//
// Each check asserts what you would see on the phone.
//
// Usage: cd tests && npm ci && npx playwright install webkit && node browser_test.mjs

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { webkit, devices } from "playwright";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PORT = 8771, ORIGIN = `http://localhost:${PORT}`, NAME = "cmux-phone test";
const state = fs.mkdtempSync(path.join(os.tmpdir(), "cmux-phone-test-"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const failures = [];

async function check(name, fn) {
  try {
    await fn();
    console.log(`✓ ${name}`);
  } catch (e) {
    failures.push(name);
    console.log(`✗ ${name}: ${e.message}`);
  }
}

function assert(ok, message) {
  if (!ok) throw new Error(message);
}

const api = async (p, body) => {
  const r = await fetch(ORIGIN + p, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
};

const server = spawn("python3", [path.join(REPO, "server.py"), "--dev"], {
  env: { ...process.env, CMUX_PHONE_PORT: String(PORT), CMUX_PHONE_STATE: state }, stdio: "inherit",
});
for (let i = 0; i < 50; i++) {
  if (await fetch(ORIGIN + "/").then((r) => r.ok, () => false)) break;
  await sleep(100);
}

const browser = await webkit.launch();
const page = await (await browser.newContext({ ...devices["iPhone 14"] })).newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
page.on("dialog", (d) => d.accept());
// The terminal's screen as text, one line per row.
const screen = () => page.evaluate(() => {
  const b = term.buffer.active, rows = [];
  for (let i = 0; i < term.rows; i++) rows.push(b.getLine(b.baseY + i).translateToString(true));
  return rows.join("\n");
});
const shows = async (text, ms = 3000) => {
  for (const end = Date.now() + ms; Date.now() < end; await sleep(100)) if ((await screen()).includes(text)) return true;
  return false;
};
let workspace = null;

try {
  await page.goto(ORIGIN);

  await check("the list shows cmux's workspaces and panes", async () => {
    await page.waitForSelector("#list-body");
    const data = await api("/api/panes");
    assert(data.sessions.filter((s) => s.current).length === 1, "no current session");
    assert((await page.locator(".pane").count()) === data.panes.length, "the list and the API disagree on the pane count");
  });

  await check("[edit] then [+ window] makes a workspace and opens its pane", async () => {
    await page.click("#edit");
    await page.click("[data-act=new-window]");
    await page.waitForSelector("#pane:not(.hidden)");
    await page.waitForFunction(() => current);
    const id = await page.evaluate(() => current);
    workspace = (await api("/api/panes")).panes.find((p) => p.id === id).window_id;
    await api(`/api/windows/${workspace}/rename`, { name: NAME });
  });
  if (!workspace) throw new Error("no test workspace, the rest can't run");

  await check("the pane on the Mac takes the phone's size", async () => {
    const [cols, rows] = await page.evaluate(() => [term.cols, term.rows]);
    await page.locator("#term").tap();
    // A pane made a moment ago takes its size a second or two after it starts, so ask until it has it.
    let sized = false;
    for (let i = 0; i < 4 && !sized; i++) {
      await page.keyboard.type("tput cols; tput lines");
      await page.keyboard.press("Enter");
      sized = await shows(`${cols}\n${rows}`, 2000);
    }
    assert(sized, `the pane is not ${cols}x${rows}: ${JSON.stringify((await screen()).trim())}`);
  });

  await check("typing reaches the pane, and its output shows", async () => {
    await page.keyboard.type("seq 1 80; echo typed-on-$((40+2))");
    await page.keyboard.press("Enter");
    assert(await shows("typed-on-42"), "the command's output never showed");
  });

  await check("a swipe scrolls back, and [↓ bottom] returns", async () => {
    await page.evaluate(() => sendWheel(true, 12, 100, 300));
    await sleep(600);
    assert(!(await screen()).includes("typed-on-42"), "still at the bottom after scrolling up");
    assert(await page.isVisible("#to-bottom"), "[↓ bottom] is not shown");
    await page.click("#to-bottom");
    assert(await shows("typed-on-42"), "not back at the bottom");
  });

  await check("[ctrl] then a key sends the control key", async () => {
    await page.click("[data-mod=ctrl]");
    await page.keyboard.type("l");
    await sleep(600);
    assert(!(await screen()).includes("typed-on-42"), "Ctrl-L did not clear the screen");
  });

  await check("a key bar key and a multi-line paste go in, the paste unsubmitted", async () => {
    const id = await page.evaluate(() => current);
    await api(`/api/panes/${id}/paste`, { text: "echo pasted-$((1+1))\necho second" });
    assert(await shows("echo second"), "the pasted text is not at the prompt");
    assert(!(await screen()).includes("pasted-2"), "the paste was submitted line by line");
    await page.click("[data-key=enter]");
    assert(await shows("pasted-2"), "[enter] did not submit it");
  });

  await check("[files] opens the pane's folder", async () => {
    await page.click("#files-open");
    await page.waitForSelector("#files:not(.hidden) .entry");
    assert((await page.textContent("#files-title")).includes(os.homedir()), "not the home folder");
    await page.click("#files-back");
    await page.waitForSelector("#pane:not(.hidden)");
  });

  await check("[‹ Panes] gives the pane back at the Mac's size", async () => {
    const id = await page.evaluate(() => current);
    await page.click("#back");
    await page.waitForSelector("#list:not(.hidden)");
    assert(!JSON.parse(fs.readFileSync(path.join(state, "fitted.json"), "utf8"))[id], "the pane is still sized to the phone");
  });

  await check("no page errors", async () => assert(!errors.length, errors.join("; ")));
} finally {
  if (workspace) await api(`/api/windows/${workspace}/close`, {});
  await browser.close();
  server.kill();
  fs.rmSync(state, { recursive: true, force: true });
}

if (failures.length) {
  console.log(`\n${failures.length} failed: ${failures.join(", ")}`);
  process.exit(1);
}
console.log("\nall checks passed");
