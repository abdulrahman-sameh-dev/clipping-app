import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createReadStream, mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const frontendRoot = join(repoRoot, "clipping_ai", "frontend");
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined;
const highlights = [
  {
    title: "First highlight",
    start_time: 0.5,
    end_time: 2.5,
    category: "insight",
  },
  {
    title: "Second highlight",
    start_time: 4,
    end_time: 7,
    category: "story",
  },
];
const job = {
  id: "job-1",
  title: "Trim editor demo",
  status: "ready",
  transcript: { language: "en" },
  metadata: { duration_seconds: 8 },
};
const clipsConfig = {
  formats: {
    vertical: { width: 1080, height: 1920 },
    square: { width: 1080, height: 1080 },
    landscape: { width: 1920, height: 1080 },
  },
  qualities: { draft: {}, standard: {}, high: {} },
  default_formats: ["vertical"],
  default_quality: "standard",
  default_fit: "crop",
  min_clip_seconds: 0.5,
  max_clip_seconds: 30,
};

const MIME = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

function serveStatic() {
  const server = createServer((request, response) => {
    const pathname = decodeURIComponent(new URL(request.url, "http://local").pathname);
    if (pathname === "/ui" || pathname === "/") {
      response.writeHead(302, { Location: "/ui/" });
      response.end();
      return;
    }
    const relative = pathname.startsWith("/ui/") ? pathname.slice(4) : pathname.slice(1);
    const filePath = normalize(join(frontendRoot, relative || "index.html"));
    if (!filePath.startsWith(frontendRoot)) {
      response.writeHead(403);
      response.end();
      return;
    }
    try {
      const stats = statSync(filePath);
      if (!stats.isFile()) throw new Error("not a file");
      response.writeHead(200, {
        "Content-Type": MIME[extname(filePath)] || "application/octet-stream",
        "Content-Length": stats.size,
      });
      createReadStream(filePath).pipe(response);
    } catch {
      response.writeHead(404);
      response.end("Not found");
    }
  });
  return new Promise((resolvePort, reject) => {
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      resolvePort({
        url: `http://127.0.0.1:${server.address().port}`,
        stop: () => new Promise((resolveStop) => server.close(resolveStop)),
      });
    });
  });
}

function makeVideo(dir) {
  const path = join(dir, "source.webm");
  const result = spawnSync(
    "ffmpeg",
    [
      "-hide_banner",
      "-loglevel",
      "error",
      "-f",
      "lavfi",
      "-i",
      "testsrc=size=320x180:rate=25",
      "-t",
      "8",
      "-c:v",
      "libvpx",
      "-b:v",
      "300k",
      "-g",
      "25",
      "-pix_fmt",
      "yuv420p",
      "-y",
      path,
    ],
    { encoding: "utf8" },
  );
  if (result.status !== 0) {
    throw new Error(`ffmpeg failed: ${result.stderr || result.stdout}`);
  }
  return readFileSync(path);
}

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(body),
  });
}

function parseRange(header, length) {
  if (!header) return null;
  const match = /^bytes=(\d*)-(\d*)$/.exec(header);
  if (!match) return null;
  let start = match[1] ? Number(match[1]) : null;
  let end = match[2] ? Number(match[2]) : null;
  if (start === null) {
    start = Math.max(0, length - (end ?? 0));
    end = length - 1;
  } else {
    end = end === null ? length - 1 : Math.min(end, length - 1);
  }
  if (start > end || start >= length) return null;
  return { start, end };
}

async function waitFor(predicate, message, timeout = 5000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolveWait) => setTimeout(resolveWait, 25));
  }
  throw new Error(message);
}

async function openExport(page, baseUrl) {
  await page.goto(`${baseUrl}/ui/#/video/job-1/export`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#trim-editor .trim-editor-grid:not([hidden])");
  await page.waitForFunction(() => document.querySelector("#trim-video")?.readyState >= 1);
}

async function createClips(page, calls) {
  const before = calls.createClips.length;
  await page.locator('[data-action="create-clips"]').click();
  await waitFor(
    () => calls.createClips.length > before,
    "Create clips request was not sent.",
  );
  await page.waitForFunction(() => window.location.hash.includes("tab=clips"));
  return calls.createClips.at(-1);
}

const tempDir = mkdtempSync(join(tmpdir(), "clipping-trim-e2e-"));
const video = makeVideo(tempDir);
const server = await serveStatic();
const browser = await chromium.launch({ headless: true, executablePath });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const calls = { createClips: [] };

await page.route("**/api/**", async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  const method = request.method();
  const path = url.pathname;

  if (path === "/api/ai/status") return json(route, { ready: true });
  if (path === "/api/clips/config") return json(route, clipsConfig);
  if (path === "/api/video/jobs/job-1" && method === "GET") return json(route, { job });
  if (path === "/api/video/jobs/job-1/highlights") {
    return json(route, { highlight_result: { highlights } });
  }
  if (path === "/api/video/jobs/job-1/transcript") return json(route, { transcript: null });
  if (path === "/api/video/jobs/job-1/clips" && method === "GET") {
    return json(route, { jobs: [] });
  }
  if (path === "/api/video/jobs/job-1/clips" && method === "POST") {
    calls.createClips.push(request.postDataJSON());
    return json(route, { jobs: [{ id: "clip-1" }] });
  }
  if (path === "/api/video/jobs/job-1/media") {
    const range = parseRange(request.headers().range, video.length);
    if (range) {
      return route.fulfill({
        status: 206,
        headers: {
          "Accept-Ranges": "bytes",
          "Content-Range": `bytes ${range.start}-${range.end}/${video.length}`,
          "Content-Length": range.end - range.start + 1,
          "Content-Type": "video/webm",
        },
        body: video.subarray(range.start, range.end + 1),
      });
    }
    return route.fulfill({
      status: 200,
      headers: {
        "Accept-Ranges": "bytes",
        "Content-Length": video.length,
        "Content-Type": "video/webm",
      },
      body: video,
    });
  }
  return json(route, { detail: `Unhandled ${method} ${path}` }, 404);
});

try {
  await openExport(page, server.url);
  assert.equal(await page.locator('input[name="highlight"]').count(), 2);
  assert.equal(await page.locator("#trim-active-title").textContent(), "First highlight");
  assert.equal(await page.locator("#trim-start-input").inputValue(), "00:00:00.500");
  assert.equal(await page.locator("#trim-end-input").inputValue(), "00:00:02.500");
  assert.equal(await page.locator("#trim-start-handle").getAttribute("aria-valuenow"), "0.5");
  assert.ok((await page.locator(".selected-highlight.is-active").count()) >= 1);

  const legacyPayload = await createClips(page, calls);
  assert.deepEqual(legacyPayload.highlight_indices, [0, 1]);
  assert.equal(legacyPayload.ranges, undefined);

  await openExport(page, server.url);
  await page.locator("#trim-start-handle").focus();
  await page.keyboard.press("Shift+ArrowRight");
  assert.equal(await page.locator("#trim-start-input").inputValue(), "00:00:01.500");

  await page.locator('[data-trim-highlight="1"]').first().click();
  assert.equal(await page.locator("#trim-active-title").textContent(), "Second highlight");
  assert.equal(await page.locator("#trim-start-input").inputValue(), "00:00:04.000");
  await page.locator('[data-trim-highlight="0"]').first().click();
  assert.equal(await page.locator("#trim-start-input").inputValue(), "00:00:01.500");

  await page.locator("#trim-play").click();
  await page.waitForFunction(() => {
    const videoElement = document.querySelector("#trim-video");
    return videoElement?.paused && videoElement.currentTime >= 2.4;
  });
  assert.ok((await page.locator("#trim-video").evaluate((videoElement) => videoElement.currentTime)) <= 2.6);

  const customPayload = await createClips(page, calls);
  assert.equal(customPayload.highlight_indices, undefined);
  assert.deepEqual(customPayload.ranges, [
    { highlight_index: 0, start_time: 1.5, end_time: 2.5 },
    { highlight_index: 1, start_time: 4, end_time: 7 },
  ]);

  await openExport(page, server.url);
  await page.locator("#trim-end-input").fill("00:00:00.200");
  await page.locator("#trim-end-input").press("Enter");
  assert.equal(await page.locator("#trim-error").textContent(), "End must be after start.");
  assert.equal(await page.locator("#trim-error").isVisible(), true);
  const beforeInvalid = calls.createClips.length;
  await page.locator('[data-action="create-clips"]').click();
  await page.waitForTimeout(150);
  assert.equal(calls.createClips.length, beforeInvalid);

  await page.locator("#trim-reset").click();
  assert.equal(await page.locator("#trim-error").isVisible(), false);
  assert.equal(await page.locator("#trim-start-input").inputValue(), "00:00:00.500");

  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator("#lang-toggle").click();
  await page.waitForFunction(() => document.documentElement.dir === "rtl");
  assert.equal(await page.locator("#trim-track").getAttribute("dir"), "ltr");
  const overflow = await page.locator("#trim-editor").evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return rect.left < -1 || rect.right > window.innerWidth + 1;
  });
  assert.equal(overflow, false, "Trim editor should fit the mobile viewport.");

  console.log("Trim editor E2E test passed.");
} finally {
  await browser.close();
  await server.stop();
  rmSync(tempDir, { recursive: true, force: true });
}
