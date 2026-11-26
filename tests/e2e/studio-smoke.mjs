import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const baseUrl = process.env.STUDIO_BASE_URL || null;

function freePort() {
  return new Promise((resolvePort, reject) => {
    const server = createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolvePort(port));
    });
  });
}

async function waitForHealth(url, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${url}/health`);
      if (response.ok) return;
    } catch {
      // Server is still starting.
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 250));
  }
  throw new Error(`Backend did not become healthy at ${url}`);
}

async function startBackend() {
  const dataDir = mkdtempSync(join(tmpdir(), "clipping-e2e-"));
  const port = await freePort();
  const logs = [];
  const child = spawn(
    "uv",
    ["run", "clipping-ai", "serve", "--host", "127.0.0.1", "--port", String(port)],
    {
      cwd: repoRoot,
      env: {
        ...process.env,
        CLIPPING_AI_DATA_DIR: dataDir,
        CLIPPING_AI_CONFIG_DIR: join(repoRoot, "config"),
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout.on("data", (chunk) => logs.push(chunk.toString()));
  child.stderr.on("data", (chunk) => logs.push(chunk.toString()));
  const url = `http://127.0.0.1:${port}`;
  try {
    await waitForHealth(url);
  } catch (error) {
    child.kill("SIGTERM");
    throw new Error(`${error.message}\n${logs.join("")}`);
  }
  return {
    url,
    stop() {
      child.kill("SIGTERM");
      rmSync(dataDir, { recursive: true, force: true });
    },
    logs: () => logs.join(""),
  };
}

async function runChecks(url) {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined;
  const browser = await chromium.launch({ headless: true, executablePath });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  const clipJobRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/clip-jobs" && url.searchParams.get("limit") === "200") {
      clipJobRequests.push(request.url());
    }
  });
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));

  try {
    await page.goto(`${url}/`, { waitUntil: "networkidle" });
    await page.waitForSelector("main h1");
    assert.equal(new URL(page.url()).pathname, "/ui/");
    assert.equal(await page.locator("main h1").textContent(), "Studio overview");
    assert.equal(await page.locator(".nav-link").count(), 7);

    await page.locator("#lang-toggle").click();
    await page.waitForFunction(() => document.documentElement.dir === "rtl");
    assert.equal(await page.locator("main h1").textContent(), "نظرة عامة على الاستوديو");
    await page.locator("#lang-toggle").click();
    await page.waitForFunction(() => document.documentElement.dir === "ltr");

    await page.goto(`${url}/ui/#/library`, { waitUntil: "networkidle" });
    assert.equal(await page.locator("main h1").textContent(), "Video library");
    await page.locator('[data-action="upload-video"]').first().click();
    await page.waitForFunction(() => document.getElementById("studio-dialog")?.open === true);
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => document.getElementById("studio-dialog")?.open === false);

    for (const [route, heading] of [
      ["#/clips", "Clip library"],
      ["#/jobs", "Jobs monitor"],
      ["#/providers", "Providers"],
    ]) {
      await page.goto(`${url}/ui/${route}`, { waitUntil: "networkidle" });
      assert.equal(await page.locator("main h1").textContent(), heading, route);
      assert.equal(await page.locator(".error-state").count(), 0, route);
      if (route === "#/clips") {
        const idleRequests = clipJobRequests.length;
        await page.waitForTimeout(4500);
        assert.equal(
          clipJobRequests.length,
          idleRequests,
          "idle Clips page should not poll clip jobs",
        );
      }
    }

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${url}/ui/`, { waitUntil: "networkidle" });
    await page.locator("#menu-button").click();
    await page.waitForFunction(() => document.getElementById("sidebar")?.classList.contains("is-open"));
    assert.equal(await page.locator("#menu-button").getAttribute("aria-expanded"), "true");
    await page.locator("#sidebar-scrim").click({ position: { x: 350, y: 400 } });
    await page.waitForFunction(
      () => !document.getElementById("sidebar")?.classList.contains("is-open"),
    );

    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
}

const backend = baseUrl ? null : await startBackend();
try {
  await runChecks(baseUrl || backend.url);
  console.log("Studio UI smoke test passed.");
} finally {
  backend?.stop();
}
