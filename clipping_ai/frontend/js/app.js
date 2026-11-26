import { api } from "./api.js";
import { getLocale, t, toggleLocale } from "./i18n.js";
import { createPoller } from "./polling.js";
import { createTrimEditor } from "./trim-editor.js";
import * as ui from "./ui.js";
import { mountYoutubePanel, youtubePanelHtml } from "./youtube.js";

const main = document.getElementById("main");
const sidebar = document.getElementById("sidebar");
const scrim = document.getElementById("sidebar-scrim");
const menuButton = document.getElementById("menu-button");
const systemPill = document.getElementById("system-pill");
const systemPillText = document.getElementById("system-pill-text");
const breadcrumb = document.getElementById("breadcrumb");

const state = {
  token: 0,
  cleanup: [],
  selection: new Map(),
  videoCache: new Map(),
  system: null,
  trimEditor: null,
};

const PROVIDER_STATUS_KEYS = {
  ready: "providers.statusReady",
  not_installed: "providers.statusNotInstalled",
  model_missing: "providers.statusModelMissing",
  misconfigured: "providers.statusMisconfigured",
  error: "providers.statusError",
  disabled: "providers.statusDisabled",
  unavailable: "providers.statusUnavailable",
};

function providerStatus(provider) {
  const status = provider.status || (provider.configured ? "ready" : "unavailable");
  return {
    key: status,
    label: t(PROVIDER_STATUS_KEYS[status] || PROVIDER_STATUS_KEYS.unavailable),
    ready: status === "ready",
  };
}

const ALLOWED_EXTENSIONS = [
  ".mp4",
  ".mkv",
  ".mov",
  ".webm",
  ".avi",
  ".m4v",
  ".mpeg",
  ".mpg",
  ".ts",
  ".flv",
  ".wmv",
];

init();

function init() {
  applyStaticTranslations();
  bindShell();
  if (!window.location.hash) {
    window.history.replaceState(null, "", "#/");
  }
  window.addEventListener("hashchange", renderRoute);
  document.addEventListener("click", handleGlobalAction);
  renderRoute();
  refreshSystem();
  window.setInterval(refreshSystem, 30000);
}

function applyStaticTranslations() {
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });
  const langLabel = document.getElementById("lang-label");
  if (langLabel) langLabel.textContent = getLocale() === "en" ? "AR" : "EN";
  const langButton = document.getElementById("lang-toggle");
  if (langButton) langButton.setAttribute("aria-label", t("common.switchLanguage"));
  const refreshButton = document.getElementById("refresh-button");
  if (refreshButton) refreshButton.setAttribute("aria-label", t("common.refreshView"));
  if (menuButton) menuButton.setAttribute("aria-label", t("common.openNavigation"));
  const skip = document.querySelector(".skip-link");
  if (skip) skip.textContent = t("common.skip");
  if (!state.system) {
    if (systemPillText) systemPillText.textContent = t("common.checking");
  }
}

function bindShell() {
  menuButton?.addEventListener("click", () => {
    const open = !sidebar?.classList.contains("is-open");
    setSidebar(open);
  });
  scrim?.addEventListener("click", () => setSidebar(false));
  document.querySelectorAll(".nav-link").forEach((link) => {
    link.addEventListener("click", () => setSidebar(false));
  });
  document.getElementById("lang-toggle")?.addEventListener("click", () => {
    toggleLocale();
    applyStaticTranslations();
    renderRoute();
    refreshSystem();
  });
  document.getElementById("refresh-button")?.addEventListener("click", () => {
    renderRoute();
    refreshSystem();
  });
  window.addEventListener("resize", () => {
    if (window.innerWidth > 900) setSidebar(false);
  });
}

function setSidebar(open) {
  sidebar?.classList.toggle("is-open", open);
  if (scrim) scrim.hidden = !open;
  menuButton?.setAttribute("aria-expanded", String(open));
  document.body.classList.toggle("nav-open", open);
}

async function refreshSystem() {
  try {
    const status = await api.status(false);
    state.system = status;
    if (systemPill) {
      systemPill.dataset.state = status.ready ? "ready" : "degraded";
      systemPill.title = status.ready ? t("providers.ready") : t("providers.notReady");
    }
    if (systemPillText) {
      systemPillText.textContent = status.ready ? t("common.online") : t("common.offline");
    }
  } catch {
    state.system = null;
    if (systemPill) {
      systemPill.dataset.state = "offline";
      systemPill.title = t("error.backend");
    }
    if (systemPillText) systemPillText.textContent = t("common.offline");
  }
}

function parseRoute() {
  const raw = window.location.hash.replace(/^#/, "") || "/";
  const [pathPart, queryPart = ""] = raw.split("?");
  const params = new URLSearchParams(queryPart);
  const segments = pathPart.split("/").filter(Boolean);
  if (!segments.length) return { name: "dashboard", params };
  const [root, id, sub] = segments;
  if (root === "library") return { name: "library", params };
  if (root === "jobs") return { name: "jobs", params };
  if (root === "clips") return { name: "clips", params };
  if (root === "clip" && id) return { name: "clip", id, params };
  if (root === "trash") return { name: "trash", params };
  if (root === "providers") return { name: "providers", params };
  if (root === "skills") return { name: "skills", params };
  if (root === "video" && id && sub === "export") return { name: "export", id, params };
  if (root === "video" && id) return { name: "analysis", id, params };
  return { name: "notfound", params };
}

async function renderRoute() {
  const token = ++state.token;
  cleanup();
  setSidebar(false);
  const route = parseRoute();
  updateNavigation(route);
  updateBreadcrumb(route);
  main.innerHTML = loadingState();
  try {
    const view = await createView(route, token);
    if (token !== state.token || !view) return;
    main.innerHTML = view.html;
    document.title = `${view.title || "Clipping Studio"} · Clipping Studio`;
    view.mount?.();
    main.focus({ preventScroll: true });
  } catch (error) {
    if (token !== state.token) return;
    main.innerHTML = ui.renderError(
      error,
      `<button class="button button-primary" type="button" data-action="retry-route">${ui.icon(
        "retry",
      )}<span>${ui.esc(t("error.retry"))}</span></button>`,
    );
  }
}

function cleanup() {
  const callbacks = state.cleanup.splice(0);
  callbacks.forEach((callback) => {
    try {
      callback();
    } catch {
      // Ignore cleanup failures from detached views.
    }
  });
}

function updateNavigation(route) {
  const active =
    route.name === "analysis" || route.name === "export"
      ? "library"
      : route.name === "clip"
        ? "clips"
        : route.name;
  document.querySelectorAll(".nav-link").forEach((link) => {
    const isActive = link.dataset.nav === active;
    link.classList.toggle("is-active", isActive);
    if (isActive) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

function updateBreadcrumb(route) {
  const items = [{ label: t("nav.dashboard"), href: "#/" }];
  if (route.name === "library") items.push({ label: t("nav.library") });
  else if (route.name === "jobs") items.push({ label: t("nav.jobs") });
  else if (route.name === "clips") items.push({ label: t("nav.clips") });
  else if (route.name === "clip") {
    items.push({ label: t("nav.clips"), href: "#/clips" });
    items.push({ label: t("clips.detailContext") });
  } else if (route.name === "trash") items.push({ label: t("nav.trash") });
  else if (route.name === "providers") items.push({ label: t("nav.providers") });
  else if (route.name === "skills") items.push({ label: t("nav.skills") });
  else if (route.name === "analysis") {
    items.push({ label: t("nav.library"), href: "#/library" });
    items.push({ label: t("analysis.context") });
  } else if (route.name === "export") {
    items.push({ label: t("nav.library"), href: "#/library" });
    items.push({ label: t("export.context") });
  } else if (route.name === "notfound") {
    items.push({ label: t("error.notFound") });
  }
  breadcrumb.innerHTML = items
    .map((item, index) => {
      const content = ui.esc(item.label);
      const part = item.href ? `<a href="${item.href}">${content}</a>` : `<span>${content}</span>`;
      return index === 0 ? part : `<span class="crumb-sep">/</span>${part}`;
    })
    .join("");
}

function loadingState() {
  return `<div class="loading-state" aria-live="polite">
    <span class="loading-mark" aria-hidden="true"></span>
    <p>${ui.esc(t("common.loadingStudio"))}</p>
  </div>`;
}

async function createView(route, token) {
  switch (route.name) {
    case "dashboard":
      return dashboardView(token);
    case "library":
      return libraryView(token);
    case "analysis":
      return analysisView(route, token);
    case "export":
      return exportView(route, token);
    case "clips":
      return clipsView(token);
    case "clip":
      return clipDetailView(route, token);
    case "trash":
      return trashView(token);
    case "jobs":
      return jobsView(token);
    case "providers":
      return providersView(token);
    case "skills":
      return skillsView(token);
    default:
      return {
        title: t("error.notFound"),
        html: `<div class="page-enter">${ui.renderError(
          { status: 404, category: "not_found", message: t("error.notFoundBody") },
          `<a class="button button-primary" href="#/">${ui.esc(t("nav.dashboard"))}</a>`,
        )}</div>`,
      };
  }
}

function go(hash) {
  if (window.location.hash === hash) renderRoute();
  else window.location.hash = hash;
}

function pageHead({ context, title, description, actions = "" }) {
  return `<div class="page-head">
    <div class="page-head-copy">
      <div class="page-context">${ui.esc(context)}</div>
      <h1>${ui.esc(title)}</h1>
      <p class="page-description">${ui.esc(description)}</p>
    </div>
    ${actions ? `<div class="page-actions">${actions}</div>` : ""}
  </div>`;
}

function primaryActions() {
  return `<button class="button button-primary" type="button" data-action="upload-video">
      ${ui.icon("upload")}<span>${ui.esc(t("common.uploadVideo"))}</span>
    </button>
    <button class="button button-secondary" type="button" data-action="import-url">
      ${ui.icon("link")}<span>${ui.esc(t("common.importUrl"))}</span>
    </button>`;
}

function startPolling(
  callback,
  { interval = 4000, token = state.token, isActive = () => true, idleInterval = null } = {},
) {
  const poller = createPoller({
    callback: async (context) => {
      if (state.token !== token) {
        poller.stop();
        return;
      }
      await callback(context);
    },
    interval,
    idleInterval,
    isActive,
    onError: (error) => {
      if (state.token === token) console.error(error);
    },
  });
  state.cleanup.push(() => poller.stop());
  return poller;
}

function isJobActive(job) {
  return job?.status === "queued" || job?.status === "processing";
}

function isClipActive(clip) {
  return isJobActive(clip) || clip?.metadata_status === "pending";
}

function sameJson(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function canRetryVideo(job) {
  return job?.status === "failed" || job?.status === "cancelled";
}

function canCancelVideo(job) {
  return job?.status === "queued";
}

function canRetryClip(clip) {
  return clip?.status === "failed" || clip?.status === "cancelled";
}

function canCancelClip(clip) {
  return (clip?.status === "queued" || clip?.status === "processing") && !clip?.cancel_requested;
}

function formatName(format) {
  const key = `export.format${format ? format[0].toUpperCase() + format.slice(1) : ""}`;
  const label = t(key);
  return label === key ? String(format || "—") : label;
}

function qualityName(quality) {
  const key = `export.quality${quality ? quality[0].toUpperCase() + quality.slice(1) : ""}`;
  const label = t(key);
  return label === key ? String(quality || "—") : label;
}

function fitName(fit) {
  const key = `export.fit${fit ? fit[0].toUpperCase() + fit.slice(1) : ""}`;
  const label = t(key);
  return label === key ? String(fit || "—") : label;
}

function sourceJobFor(clip) {
  return state.videoCache.get(clip?.source_job_id) || null;
}

function highlightFor(clip, sourceJob = sourceJobFor(clip)) {
  const highlights = sourceJob?.highlight_result?.highlights || [];
  if (clip?.highlight_index === null || clip?.highlight_index === undefined) return null;
  return highlights[clip.highlight_index] || null;
}

function clipTitle(clip, sourceJob = sourceJobFor(clip)) {
  if (clip?.title) return clip.title;
  const highlight = highlightFor(clip, sourceJob);
  if (highlight?.title) return highlight.title;
  const number = (clip?.highlight_index ?? 0) + 1;
  return `${t("common.clip")} ${number}`;
}

function clipDetailHref(clip) {
  return `#/clip/${encodeURIComponent(clip.id)}`;
}

function metadataStatusLabel(status) {
  if (!status) return t("clips.metadataNone");
  return t(`clips.metadata_${status}`);
}

function clipPublishBadge(clip) {
  return clip.published
    ? `<span class="tag is-teal">${ui.icon("check")}${ui.esc(t("clips.published"))}</span>`
    : `<span class="tag">${ui.esc(t("clips.unpublished"))}</span>`;
}

function clipDate(clip) {
  return clip.completed_at || clip.updated_at || clip.created_at;
}

function clipPublicMeta(clip) {
  return [
    formatName(clip.format),
    ui.formatDuration(clip.duration),
    qualityName(clip.quality),
    clip.captions ? t("common.captions") : null,
    clipDate(clip) ? ui.formatDateTimeLabel(clipDate(clip)) : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

function clipMeta(clip) {
  return [
    formatName(clip.format),
    `${clip.width}×${clip.height}`,
    ui.formatDuration(clip.duration),
    qualityName(clip.quality),
    clip.captions ? t("common.captions") : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

function highlightExcerpt(highlight) {
  if (!highlight) return "";
  if (highlight.description) return highlight.description;
  if (highlight.transcript_segments?.length) {
    return highlight.transcript_segments.map((segment) => segment.text).join(" ");
  }
  return highlight.selection_reason || "";
}

function exportHighlightRow(
  highlight,
  index,
  { summary = false, checked = true, active = false, range = null, custom = false } = {},
) {
  const start = Number(range?.start ?? highlight.start_time ?? 0);
  const end = Number(range?.end ?? highlight.end_time ?? start);
  const id = summary ? `export-summary-highlight-${index}` : `export-highlight-${index}`;
  const meta = [highlight.category, custom ? t("trim.custom") : ""]
    .filter(Boolean)
    .join(" · ");
  const input = summary
    ? `<input type="checkbox" id="${id}" data-summary-highlight="${index}" ${
        checked ? "checked" : ""
      } />`
    : `<input type="checkbox" id="${id}" name="highlight" value="${index}" ${
        checked ? "checked" : ""
      } />`;
  return `<div class="selected-highlight ${active ? "is-active" : ""} ${
    custom ? "is-custom" : ""
  }" data-trim-row="${index}">
    ${input}
    <label class="selected-highlight-copy" for="${id}">
      <strong>${ui.esc(highlight.title)}</strong>
      ${meta ? `<small>${ui.esc(meta)}</small>` : ""}
    </label>
    <span class="duration" dir="ltr">${ui.esc(ui.formatDuration(end - start))}</span>
    <button class="icon-button trim-row-button" type="button" data-trim-highlight="${index}" aria-pressed="${active}" aria-label="${ui.attr(
      t("trim.openNamed", { title: highlight.title }),
    )}" title="${ui.attr(t("trim.open"))}">${ui.icon("sliders")}</button>
  </div>`;
}

function selectionFor(jobId, count) {
  let selected = state.selection.get(jobId);
  if (!selected) {
    selected = new Set(Array.from({ length: count }, (_, index) => index));
    state.selection.set(jobId, selected);
  } else {
    for (const index of [...selected]) {
      if (index >= count) selected.delete(index);
    }
  }
  return selected;
}

function compactProgress(status, progress, stage) {
  const value = Math.max(0, Math.min(1, Number(progress) || 0));
  const stateClass =
    status === "ready" ? "is-ready" : status === "failed" ? "is-failed" : status === "processing" ? "is-processing" : "";
  return `<div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(
    value * 100,
  )}" aria-label="${ui.attr(ui.statusLabel(status))}">
      <div class="progress-fill ${stateClass}" style="width:${value * 100}%"></div>
    </div>
    <span dir="ltr">${ui.esc(stage ? ui.formatStage(stage) : ui.formatPercent(value))}</span>`;
}

function jobRow(job, { selectable = false, selected = false } = {}) {
  const retry = canRetryVideo(job)
    ? `<button class="icon-button button-small" type="button" data-action="retry-video" data-job-id="${ui.attr(
        job.id,
      )}" title="${ui.attr(t("library.retryJob"))}" aria-label="${ui.attr(t("library.retryJob"))}">${ui.icon(
        "retry",
      )}</button>`
    : "";
  const cancel = canCancelVideo(job)
    ? `<button class="icon-button button-small" type="button" data-action="cancel-video" data-job-id="${ui.attr(
        job.id,
      )}" title="${ui.attr(t("library.cancelJob"))}" aria-label="${ui.attr(t("library.cancelJob"))}">${ui.icon(
        "cancel",
      )}</button>`
    : "";
  const checkbox = selectable
    ? `<label class="row-select" title="${ui.attr(t("common.select"))}"><input type="checkbox" data-select-job="${ui.attr(
        job.id,
      )}" ${selected ? "checked" : ""} aria-label="${ui.attr(t("common.select"))}"/></label>`
    : "";
  return `<div class="job-row" data-job-id="${ui.attr(job.id)}">
    <div class="job-row-main">
      ${checkbox}
      <div>
        <a class="job-title" href="#/video/${ui.attr(job.id)}" title="${ui.attr(ui.jobTitle(job))}">${ui.esc(
          ui.jobTitle(job),
        )}</a>
        <div class="job-sub">${ui.esc(ui.jobSubtitle(job))}</div>
      </div>
    </div>
    <span class="job-cell" dir="ltr">${
      job.metadata?.duration_seconds ? ui.formatDuration(job.metadata.duration_seconds) : "—"
    }</span>
    <span>${ui.statusPill(job.status)}</span>
    <div class="job-publish">
      <label class="source-publish">
        <input type="checkbox" data-publish-source="${ui.attr(job.id)}" ${
          job.all_clips_published ? "checked" : ""
        } aria-label="${ui.attr(t("library.allPublished"))}"/>
        <span>${ui.esc(
          job.all_clips_published ? t("library.allPublished") : t("library.notAllPublished"),
        )}</span>
      </label>
      <small>${ui.esc(
        t("library.publishedCount", {
          published: ui.formatNumberLabel(job.clip_counts?.published || 0),
          total: ui.formatNumberLabel(job.clip_counts?.total || 0),
        }),
      )}</small>
    </div>
    <div class="job-row-progress">${compactProgress(job.status, job.progress, job.stage)}</div>
    <div class="job-actions">
      ${retry}${cancel}
      ${
        selectable
          ? `<button class="icon-button button-small" type="button" data-action="delete-video" data-job-id="${ui.attr(
              job.id,
            )}" title="${ui.attr(t("common.delete"))}" aria-label="${ui.attr(t("common.delete"))}">${ui.icon(
              "trash",
            )}</button>`
          : ""
      }
      <a class="row-chevron" href="#/video/${ui.attr(job.id)}" aria-label="${ui.attr(
        t("library.openAnalysis"),
      )}">${ui.icon("chevron")}</a>
    </div>
  </div>`;
}

function clipCard(clip, { selectable = false, selected = false } = {}) {
  const sourceJob = sourceJobFor(clip);
  const title = clipTitle(clip, sourceJob);
  const downloadUrl = api.clipDownloadUrl(clip.id);
  const isReady = clip.status === "ready";
  const mediaClass = clip.format ? `is-${clip.format}` : "";
  const media = isReady
    ? `<video src="${downloadUrl}#t=0.1" preload="metadata" muted playsinline aria-hidden="true"></video>`
    : `<div class="clip-media-placeholder">${ui.icon("film")}</div>`;
  const checkbox = selectable
    ? `<label class="card-select" title="${ui.attr(t("common.select"))}"><input type="checkbox" data-select-clip="${ui.attr(
        clip.id,
      )}" ${selected ? "checked" : ""} aria-label="${ui.attr(t("common.select"))}"/></label>`
    : "";
  const actions = [
    `<a class="button button-secondary button-small" href="${clipDetailHref(clip)}">${ui.icon(
      "sliders",
    )}<span>${ui.esc(t("clips.openDetail"))}</span></a>`,
    `<button class="button button-secondary button-small" type="button" data-action="preview-clip" data-clip-id="${ui.attr(
      clip.id,
    )}">${ui.icon("play")}<span>${ui.esc(t("clips.preview"))}</span></button>`,
    isReady
      ? `<a class="button button-primary button-small" href="${downloadUrl}" download>${ui.icon(
          "download",
        )}<span>${ui.esc(t("common.download"))}</span></a>`
      : "",
    clip.status === "queued"
      ? `<button class="button button-secondary button-small" type="button" data-action="render-clip" data-clip-id="${ui.attr(
          clip.id,
        )}">${ui.icon("spark")}<span>${ui.esc(t("common.renderNow"))}</span></button>`
      : "",
    canRetryClip(clip)
      ? `<button class="button button-secondary button-small" type="button" data-action="retry-clip" data-clip-id="${ui.attr(
          clip.id,
        )}">${ui.icon("retry")}<span>${ui.esc(t("clips.retry"))}</span></button>`
      : "",
    canCancelClip(clip)
      ? `<button class="button button-danger button-small" type="button" data-action="cancel-clip" data-clip-id="${ui.attr(
          clip.id,
        )}">${ui.icon("cancel")}<span>${ui.esc(t("clips.cancel"))}</span></button>`
      : "",
    `<button class="button button-ghost button-small" type="button" data-action="delete-clip" data-clip-id="${ui.attr(
      clip.id,
    )}">${ui.icon("trash")}<span>${ui.esc(t("common.delete"))}</span></button>`,
  ]
    .filter(Boolean)
    .join("");
  const errorText = clip.error?.message
    ? `<p class="small muted" title="${ui.attr(clip.error.message)}">${ui.esc(
        String(clip.error.message).slice(0, 100),
      )}</p>`
    : "";
  const metadata = clip.metadata_status
    ? `<span class="tag">${ui.esc(metadataStatusLabel(clip.metadata_status))}</span>`
    : "";
  return `<article class="clip-card ${selected ? "is-selected" : ""}" data-clip-id="${ui.attr(clip.id)}">
    <div class="clip-media ${mediaClass}">
      ${media}
      ${checkbox}
      <div class="clip-media-badges">
        <span>${ui.esc(formatName(clip.format))}</span>
        <span>${clip.captions ? "CC" : ui.esc(qualityName(clip.quality))}</span>
      </div>
    </div>
    <div class="clip-body">
      <h3><a href="${clipDetailHref(clip)}">${ui.esc(title)}</a></h3>
      <div class="clip-meta">${ui.esc(clipPublicMeta(clip))}</div>
      <div class="signal-list compact">
        ${ui.statusPill(clip.status)}
        ${clipPublishBadge(clip)}
        ${metadata}
      </div>
      <div class="section-title" style="margin:0">
        <a class="text-link small" href="#/video/${ui.attr(clip.source_job_id)}">${ui.esc(
          t("clips.openSource"),
        )}</a>
      </div>
      ${
        clip.status === "processing" || clip.status === "queued"
          ? compactProgress(clip.status, clip.progress, clip.stage)
          : ""
      }
      ${errorText}
      <div class="clip-card-actions">${actions}</div>
    </div>
  </article>`;
}

function miniClip(clip) {
  const sourceJob = sourceJobFor(clip);
  const title = clipTitle(clip, sourceJob);
  const isReady = clip.status === "ready";
  return `<a class="mini-clip" href="${clipDetailHref(clip)}" title="${ui.attr(title)}">
    <div class="mini-media">
      ${
        isReady
          ? `<video src="${api.clipDownloadUrl(clip.id)}#t=0.1" preload="metadata" muted playsinline></video>`
          : ""
      }
      <span class="mini-media-badge">${ui.esc(formatName(clip.format))}</span>
    </div>
    <strong>${ui.esc(title)}</strong>
    <small>${ui.esc(ui.formatDuration(clip.duration))} · ${ui.esc(ui.statusLabel(clip.status))}</small>
  </a>`;
}

async function loadSourceJobs(clips, signal) {
  const ids = [...new Set(clips.map((clip) => clip.source_job_id).filter(Boolean))];
  const missing = ids.filter((id) => !state.videoCache.has(id)).slice(0, 30);
  await Promise.allSettled(
    missing.map(async (id) => {
      try {
        const job = await api.getVideoJob(id, { signal });
        state.videoCache.set(id, job);
      } catch (error) {
        if (error?.name !== "AbortError") state.videoCache.set(id, null);
      }
    }),
  );
}

async function dashboardView(token) {
  const [statusResult, jobsResult, clipsResult] = await Promise.allSettled([
    api.status(false),
    api.listVideoJobs({ limit: 100 }),
    api.listClipJobs({ limit: 100 }),
  ]);
  const status = statusResult.status === "fulfilled" ? statusResult.value : state.system;
  const jobs = jobsResult.status === "fulfilled" ? jobsResult.value.jobs || [] : [];
  const clips = clipsResult.status === "fulfilled" ? clipsResult.value.jobs || [] : [];
  if (!status && jobsResult.status === "rejected" && clipsResult.status === "rejected") {
    throw jobsResult.reason || clipsResult.reason;
  }
  await loadSourceJobs(clips);
  const activeJobs = jobs.filter((job) => job.status === "processing" || job.status === "queued");
  const activeClips = clips.filter((clip) => clip.status === "processing" || clip.status === "queued");
  const readyClips = clips.filter((clip) => clip.status === "ready");
  const configuredProviders = (status?.providers || []).filter((provider) => provider.configured).length;
  const providerCount = status?.providers?.length || 0;
  const freeModels = status?.models?.free_approved ?? 0;
  const activeVideo = activeJobs[0];
  const activeClip = activeClips[0];
  const readyToClip = jobs.find(
    (job) => job.status === "ready" && job.highlight_result && !clips.some((clip) => clip.source_job_id === job.id),
  );
  const active = activeVideo
    ? { kind: "video", item: activeVideo }
    : activeClip
      ? { kind: "clip", item: activeClip }
      : readyToClip
        ? { kind: "ready", item: readyToClip }
        : null;

  const signals = [
    {
      label: t("dashboard.signalService"),
      value: status?.ready ? t("common.online") : t("common.offline"),
      small: status?.local_only ? t("nav.localOnly") : "",
    },
    {
      label: t("dashboard.signalProviders"),
      value: `${ui.formatNumberLabel(configuredProviders)}/${ui.formatNumberLabel(providerCount)}`,
      small: t("common.configured"),
    },
    {
      label: t("dashboard.signalModels"),
      value: ui.formatNumberLabel(freeModels),
      small: t("providers.freeApproved"),
    },
    {
      label: t("dashboard.signalQueue"),
      value: ui.formatNumberLabel(activeJobs.length + activeClips.length),
      small: t("common.active"),
    },
    {
      label: t("dashboard.signalClips"),
      value: ui.formatNumberLabel(readyClips.length),
      small: t("common.ready"),
    },
  ];

  const activeHtml = active
    ? active.kind === "video"
      ? `<div class="active-job">
          <div class="active-job-top">
            <div>
              <h3>${ui.esc(ui.jobTitle(active.item))}</h3>
              <div class="active-job-meta">
                <span>${ui.esc(ui.formatStage(active.item.stage))}</span>
                <span dir="ltr">${ui.formatPercent(active.item.progress)}</span>
                <span>${ui.esc(ui.jobSubtitle(active.item))}</span>
              </div>
            </div>
            ${ui.statusPill(active.item.status)}
          </div>
          ${ui.progressBar(active.item.progress, active.item.status, active.item.stage)}
          <div class="page-actions" style="justify-content:flex-start">
            <a class="button button-secondary button-small" href="#/video/${ui.attr(active.item.id)}">${ui.icon(
              "arrow",
            )}<span>${ui.esc(t("library.openAnalysis"))}</span></a>
            <a class="button button-ghost button-small" href="#/jobs">${ui.esc(
              t("dashboard.openMonitor"),
            )}</a>
          </div>
        </div>`
      : active.kind === "clip"
        ? `<div class="active-job">
            <div class="active-job-top">
              <div>
                <h3>${ui.esc(clipTitle(active.item))}</h3>
                <div class="active-job-meta">
                  <span>${ui.esc(formatName(active.item.format))}</span>
                  <span>${ui.esc(ui.formatStage(active.item.stage))}</span>
                  <span dir="ltr">${ui.formatPercent(active.item.progress)}</span>
                </div>
              </div>
              ${ui.statusPill(active.item.status)}
            </div>
            ${ui.progressBar(active.item.progress, active.item.status, active.item.stage)}
            <div class="page-actions" style="justify-content:flex-start">
              <a class="button button-secondary button-small" href="#/clips">${ui.icon(
                "arrow",
              )}<span>${ui.esc(t("dashboard.allClips"))}</span></a>
            </div>
          </div>`
        : `<div class="active-job">
            <div class="active-job-top">
              <div>
                <h3>${ui.esc(ui.jobTitle(active.item))}</h3>
                <div class="active-job-meta"><span>${ui.esc(t("dashboard.readyToClip"))}</span></div>
              </div>
              ${ui.statusPill("ready")}
            </div>
            <p class="muted small">${ui.esc(t("analysis.exportHint"))}</p>
            <div class="page-actions" style="justify-content:flex-start">
              <a class="button button-primary button-small" href="#/video/${ui.attr(
                active.item.id,
              )}?tab=highlights">${ui.icon("spark")}<span>${ui.esc(t("analysis.exportClips"))}</span></a>
            </div>
          </div>`
    : ui.renderEmpty({
        iconName: "spark",
        title: t("dashboard.noActive"),
        body: t("dashboard.noActiveBody"),
        action: primaryActions(),
      });

  const providerRows = (status?.providers || [])
    .slice(0, 4)
    .map((provider) => {
      const providerState = providerStatus(provider);
      return `<div class="provider-row">
        <span class="provider-mark ${providerState.ready ? "is-ready" : ""}">${ui.esc(
          provider.id.slice(0, 2).toUpperCase(),
        )}</span>
        <div class="provider-copy">
          <strong>${ui.esc(provider.id)}</strong>
          <span>${ui.esc(provider.type)} · ${ui.formatNumberLabel(provider.models)} ${ui.esc(
            t("common.models"),
          )}</span>
        </div>
        <span class="provider-state ${providerState.ready ? "is-ready" : ""}">${ui.esc(
          providerState.label,
        )}</span>
      </div>`;
    })
    .join("");

  const recentVideos = jobs.slice(0, 5);
  const latestClips = clips
    .slice()
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
    .slice(0, 3);

  const html = `<div class="page-enter">
    ${pageHead({
      context: t("dashboard.context"),
      title: t("dashboard.title"),
      description: t("dashboard.description"),
      actions: primaryActions(),
    })}
    <div class="signal-rail">
      ${signals
        .map(
          (signal) => `<div class="signal">
            <span class="signal-label">${ui.esc(signal.label)}</span>
            <span class="signal-value">${ui.esc(signal.value)}${
              signal.small ? `<small>${ui.esc(signal.small)}</small>` : ""
            }</span>
          </div>`,
        )
        .join("")}
    </div>
    <div class="dashboard-grid">
      <section class="panel panel-full">
        <div class="panel-header">
          <div>
            <h2>${ui.esc(t("dashboard.activePipeline"))}</h2>
            <p>${ui.esc(t("dashboard.workerNote"))}</p>
          </div>
          <a class="panel-link" href="#/jobs">${ui.esc(t("dashboard.openMonitor"))}${ui.icon(
            "arrow",
          )}</a>
        </div>
        <div class="panel-body">${activeHtml}</div>
      </section>
      <section class="panel">
        <div class="panel-header">
          <h2>${ui.esc(t("dashboard.recentVideos"))}</h2>
          <a class="panel-link" href="#/library">${ui.esc(t("dashboard.allVideos"))}${ui.icon(
            "arrow",
          )}</a>
        </div>
        <div class="panel-body">
          ${
            recentVideos.length
              ? `<div class="row-list">${recentVideos.map(jobRow).join("")}</div>`
              : ui.renderEmpty({
                  iconName: "library",
                  title: t("dashboard.noVideos"),
                  body: t("dashboard.noVideosBody"),
                })
          }
        </div>
      </section>
      <section class="panel">
        <div class="panel-header">
          <h2>${ui.esc(t("dashboard.providers"))}</h2>
          <a class="panel-link" href="#/providers">${ui.esc(t("dashboard.manageProviders"))}${ui.icon(
            "arrow",
          )}</a>
        </div>
        <div class="panel-body">
          ${
            providerRows
              ? `<div class="provider-list">${providerRows}</div>`
              : ui.renderEmpty({
                  iconName: "providers",
                  title: t("providers.notReady"),
                  body: t("providers.notReadyBody"),
                })
          }
        </div>
      </section>
      <section class="panel panel-full">
        <div class="panel-header">
          <h2>${ui.esc(t("dashboard.latestClips"))}</h2>
          <a class="panel-link" href="#/clips">${ui.esc(t("dashboard.allClips"))}${ui.icon("arrow")}</a>
        </div>
        <div class="panel-body">
          ${
            latestClips.length
              ? `<div class="filmstrip">${latestClips.map(miniClip).join("")}</div>`
              : ui.renderEmpty({
                  iconName: "clips",
                  title: t("dashboard.noClips"),
                  body: t("dashboard.noClipsBody"),
                  action: `<a class="button button-secondary button-small" href="#/library">${ui.esc(
                    t("nav.library"),
                  )}</a>`,
                })
          }
        </div>
      </section>
    </div>
  </div>`;

  return {
    title: t("dashboard.title"),
    html,
    mount() {
      const wasActive = jobs.some(isJobActive) || clips.some(isClipActive);
      startPolling(
        async ({ signal }) => {
          const [nextStatus, nextJobs, nextClips] = await Promise.allSettled([
            api.status(false, { signal }),
            api.listVideoJobs({ limit: 100 }, { signal }),
            api.listClipJobs({ limit: 100 }, { signal }),
          ]);
          const stillActive =
            (nextJobs.status === "fulfilled" &&
              (nextJobs.value.jobs || []).some(isJobActive)) ||
            (nextClips.status === "fulfilled" &&
              (nextClips.value.jobs || []).some(isClipActive));
          if (stillActive || wasActive || nextStatus.status === "rejected") renderRoute();
        },
        { interval: 4000, token, isActive: () => wasActive },
      );
    },
  };
}

async function libraryView(token) {
  const data = await api.listVideoJobs({ limit: 200 });
  let jobs = data.jobs || [];
  let filter = "all";
  let searchValue = "";
  const selected = new Set();

  const html = `<div class="page-enter">
    ${pageHead({
      context: t("dashboard.context"),
      title: t("library.title"),
      description: t("library.description"),
      actions: primaryActions(),
    })}
    <div class="toolbar toolbar-wrap">
      <div class="search-field">
        ${ui.icon("search")}
        <input id="library-search" type="search" autocomplete="off" placeholder="${ui.attr(
          t("library.searchPlaceholder"),
        )}" aria-label="${ui.attr(t("common.search"))}" />
      </div>
      <div class="toolbar-actions">
        <button class="button button-ghost button-small" type="button" id="library-select-all">${ui.icon(
          "check",
        )}<span>${ui.esc(t("common.selectAll"))}</span></button>
        <button class="button button-ghost button-small" type="button" id="library-clear">${ui.icon(
          "cancel",
        )}<span>${ui.esc(t("common.clear"))}</span></button>
        <button class="button button-danger button-small" type="button" id="library-delete-selected" disabled>${ui.icon(
          "trash",
        )}<span id="library-delete-label">${ui.esc(t("common.delete"))}</span></button>
        <a class="button button-secondary button-small" href="#/trash">${ui.icon(
          "trash",
        )}<span>${ui.esc(t("nav.trash"))}</span></a>
      </div>
      <span class="small muted" id="library-count"></span>
    </div>
    <div class="filter-row" id="library-filters" role="group" aria-label="${ui.attr(
      t("common.status"),
    )}"></div>
    <div class="list-surface" style="margin-block-start:14px">
      <div class="list-head">
        <span>${ui.esc(t("common.video"))}</span>
        <span>${ui.esc(t("common.duration"))}</span>
        <span>${ui.esc(t("common.status"))}</span>
        <span>${ui.esc(t("library.publishing"))}</span>
        <span>${ui.esc(t("common.progress"))}</span>
        <span style="text-align:end">${ui.esc(t("common.actions"))}</span>
      </div>
      <div class="list-body" id="library-list" aria-live="polite"></div>
    </div>
  </div>`;

  function filteredJobs() {
    const query = searchValue.trim().toLowerCase();
    return jobs.filter((job) => {
      if (filter === "completed" && !job.all_clips_published) return false;
      if (filter === "incomplete" && job.all_clips_published) return false;
      if (!["all", "completed", "incomplete"].includes(filter) && job.status !== filter) {
        return false;
      }
      if (!query) return true;
      return [job.id, job.original_filename, job.source_value, job.metadata?.format_name]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query));
    });
  }

  function paintFilters() {
    const counts = { all: jobs.length };
    for (const status of ["ready", "processing", "queued", "failed", "cancelled"]) {
      counts[status] = jobs.filter((job) => job.status === status).length;
    }
    counts.completed = jobs.filter((job) => job.all_clips_published).length;
    counts.incomplete = jobs.filter((job) => !job.all_clips_published).length;
    const holder = main.querySelector("#library-filters");
    if (!holder) return;
    const labels = {
      all: t("common.all"),
      completed: t("library.completed"),
      incomplete: t("library.incomplete"),
    };
    holder.innerHTML = Object.entries(counts)
      .map(
        ([status, count]) => `<button class="filter-chip ${filter === status ? "is-active" : ""}" type="button" data-filter="${status}" aria-pressed="${
          filter === status
        }">${ui.esc(labels[status] || ui.statusLabel(status))}<small>${ui.formatNumberLabel(
          count,
        )}</small></button>`,
      )
      .join("");
  }

  function paintSelection() {
    const ids = new Set(jobs.map((job) => job.id));
    for (const id of [...selected]) if (!ids.has(id)) selected.delete(id);
    const button = main.querySelector("#library-delete-selected");
    const label = main.querySelector("#library-delete-label");
    if (button) button.disabled = selected.size === 0;
    if (label) {
      label.textContent = selected.size
        ? t("common.deleteCount", { count: selected.size })
        : t("common.delete");
    }
  }

  function paintList() {
    const holder = main.querySelector("#library-list");
    const countHolder = main.querySelector("#library-count");
    if (!holder) return;
    const rows = filteredJobs();
    if (countHolder) {
      countHolder.textContent = `${ui.formatNumberLabel(rows.length)} / ${ui.formatNumberLabel(
        jobs.length,
      )}`;
    }
    if (!jobs.length) {
      holder.innerHTML = ui.renderEmpty({
        iconName: "library",
        title: t("library.emptyTitle"),
        body: t("library.emptyBody"),
        action: primaryActions(),
      });
    } else if (!rows.length) {
      holder.innerHTML = ui.renderEmpty({
        iconName: "search",
        title: t("library.filteredEmpty"),
        body: t("library.searchPlaceholder"),
      });
    } else {
      holder.innerHTML = `<div class="row-list">${rows
        .map((job) => jobRow(job, { selectable: true, selected: selected.has(job.id) }))
        .join("")}</div>`;
    }
    paintFilters();
    paintSelection();
  }

  async function deleteSelected() {
    if (!selected.size) return;
    const confirmed = await ui.confirmDialog(
      t("library.bulkDeleteConfirm", { count: selected.size }),
      {
        title: t("library.bulkDelete"),
        confirmLabel: t("common.delete"),
        danger: true,
      },
    );
    if (!confirmed) return;
    const ids = [...selected];
    const results = await Promise.allSettled(ids.map((id) => api.deleteVideo(id)));
    const failures = results.filter((result) => result.status === "rejected").length;
    if (failures) {
      ui.toast(t("library.bulkDeletePartial", { count: failures }), "warning");
    } else {
      ui.toast(t("library.movedToTrash", { count: ids.length }), "success");
    }
    renderRoute();
  }

  return {
    title: t("library.title"),
    html,
    mount() {
      paintList();
      const search = main.querySelector("#library-search");
      search?.addEventListener("input", () => {
        searchValue = search.value;
        paintList();
      });
      main.querySelector("#library-filters")?.addEventListener("click", (event) => {
        const chip = event.target.closest("[data-filter]");
        if (!chip) return;
        filter = chip.dataset.filter;
        paintList();
      });
      main.querySelector("#library-select-all")?.addEventListener("click", () => {
        filteredJobs().forEach((job) => selected.add(job.id));
        paintList();
      });
      main.querySelector("#library-clear")?.addEventListener("click", () => {
        selected.clear();
        paintList();
      });
      main.querySelector("#library-delete-selected")?.addEventListener("click", deleteSelected);
      main.querySelector("#library-list")?.addEventListener("change", (event) => {
        const checkbox = event.target.closest("[data-select-job]");
        if (!checkbox) return;
        if (checkbox.checked) selected.add(checkbox.dataset.selectJob);
        else selected.delete(checkbox.dataset.selectJob);
        paintSelection();
      });
      main.querySelector("#library-list")?.addEventListener("change", async (event) => {
        const checkbox = event.target.closest("[data-publish-source]");
        if (!checkbox) return;
        const jobId = checkbox.dataset.publishSource;
        const published = checkbox.checked;
        checkbox.disabled = true;
        try {
          const result = await api.setSourcePublished(jobId, published);
          jobs = jobs.map((job) => (job.id === jobId ? result.job : job));
          paintList();
          ui.toast(t(published ? "library.sourcePublished" : "library.sourceUnpublished"), "success");
        } catch {
          checkbox.checked = !published;
          checkbox.disabled = false;
          ui.toast(t("library.sourcePublishFailed"), "error");
        }
      });
      startPolling(
        async ({ signal }) => {
          const next = await api.listVideoJobs({ limit: 200 }, { signal });
          const nextJobs = next.jobs || [];
          if (sameJson(nextJobs, jobs)) return;
          jobs = nextJobs;
          paintList();
        },
        { interval: 4000, token, isActive: () => jobs.some(isJobActive) },
      );
    },
  };
}

async function analysisView(route, token) {
  const jobId = route.id;
  const tab = route.params.get("tab") || "overview";
  const [job, transcriptPayload, highlightsPayload, clipsPayload, config] = await Promise.all([
    api.getVideoJob(jobId),
    api.getTranscript(jobId),
    api.getHighlights(jobId),
    api.listSourceClips(jobId, 100),
    api.analysisConfig(),
  ]);
  state.videoCache.set(job.id, job);
  const transcript = transcriptPayload.transcript || job.transcript || null;
  const highlightResult = highlightsPayload.highlight_result || job.highlight_result || null;
  const highlights = highlightResult?.highlights || [];
  const clips = clipsPayload.jobs || [];
  await loadSourceJobs(clips);
  const selected = selectionFor(job.id, highlights.length);
  const contentTypes = ["auto", "podcast", "educational", "general", "gaming", "live", "mixed", "unknown"];
  const classification = highlightResult?.classification;
  const signals = highlightResult?.signals || {};
  const transcriptSegments = transcript?.segments || [];
  const words = transcript?.words?.length || 0;

  const prepareState =
    job.status === "ready" ? "is-done" : job.status === "failed" ? "is-failed" : "is-active";
  const transcriptState = transcript ? "is-done" : job.transcript_error ? "is-failed" : "";
  const highlightState = highlightResult
    ? "is-done"
    : job.highlight_error
      ? "is-failed"
      : transcript
        ? "is-active"
        : "";
  const exportState = clips.length ? "is-done" : highlights.length ? "is-active" : "";

  const pipeline = `<div class="pipeline" aria-label="${ui.attr(t("analysis.pipeline"))}">
    <div class="pipeline-step ${prepareState}"><span>01</span><strong>${ui.esc(
      t("analysis.stepPrepare"),
    )}</strong><small class="muted">${ui.esc(ui.statusLabel(job.status))}</small></div>
    <div class="pipeline-step ${transcriptState}"><span>02</span><strong>${ui.esc(
      t("analysis.stepTranscribe"),
    )}</strong><small class="muted">${ui.esc(
      transcript ? t("common.ready") : job.transcript_error ? t("common.failed") : t("common.queued"),
    )}</small></div>
    <div class="pipeline-step ${highlightState}"><span>03</span><strong>${ui.esc(
      t("analysis.stepHighlights"),
    )}</strong><small class="muted">${ui.esc(
      highlightResult
        ? `${ui.formatNumberLabel(highlights.length)} ${t("common.highlights")}`
        : job.highlight_error
          ? t("common.failed")
          : t("common.queued"),
    )}</small></div>
    <div class="pipeline-step ${exportState}"><span>04</span><strong>${ui.esc(
      t("analysis.stepExport"),
    )}</strong><small class="muted">${ui.esc(
      clips.length
        ? `${ui.formatNumberLabel(clips.length)} ${t("common.clips")}`
        : t("common.queued"),
    )}</small></div>
  </div>`;

  const tabs = [
    ["overview", t("common.overview"), ""],
    ["transcript", t("common.transcript"), transcriptSegments.length || words],
    ["highlights", t("common.highlights"), highlights.length],
    ["clips", t("common.clips"), clips.length],
  ];
  const tabsHtml = `<div class="tabs" role="tablist" aria-label="${ui.attr(
    t("analysis.context"),
  )}">
    ${tabs
      .map(
        ([name, label, count]) => `<a class="tab ${tab === name ? "is-active" : ""}" href="#/video/${ui.attr(
          job.id,
        )}?tab=${name}" role="tab" aria-selected="${tab === name}">${ui.esc(label)}${
          count !== "" ? `<small>${ui.formatNumberLabel(count)}</small>` : ""
        }</a>`,
      )
      .join("")}
  </div>`;

  const actions = `<a class="button button-secondary" href="#/library">${ui.icon(
    "arrow",
  )}<span>${ui.esc(t("common.back"))}</span></a>
    ${
      canRetryVideo(job)
        ? `<button class="button button-secondary" type="button" data-action="retry-video" data-job-id="${ui.attr(
            job.id,
          )}">${ui.icon("retry")}<span>${ui.esc(t("common.retry"))}</span></button>`
        : ""
    }
    ${
      canCancelVideo(job)
        ? `<button class="button button-danger" type="button" data-action="cancel-video" data-job-id="${ui.attr(
            job.id,
          )}">${ui.icon("cancel")}<span>${ui.esc(t("common.cancel"))}</span></button>`
        : ""
    }
    ${
      highlights.length
        ? `<a class="button button-primary" href="#/video/${ui.attr(job.id)}/export">${ui.icon(
            "clips",
          )}<span>${ui.esc(t("analysis.exportClips"))}</span></a>`
        : ""
    }`;

  const header = `<div class="detail-head page-enter">
    <div>
      <div class="page-context">${ui.esc(t("analysis.context"))}</div>
      <h1>${ui.esc(ui.jobTitle(job))}</h1>
      <div class="detail-meta">
        <span>${ui.esc(ui.statusLabel(job.status))}</span>
        <span>${ui.esc(ui.formatStage(job.stage))}</span>
        <span dir="ltr">${ui.esc(ui.formatPercent(job.progress))}</span>
        <span>${ui.esc(ui.jobSubtitle(job))}</span>
      </div>
    </div>
    <div class="detail-actions">${actions}</div>
  </div>`;

  const metadata = job.metadata;
  const stream = metadata?.video_streams?.[0];
  const sourceInfo = `<div class="info-grid">
    <div class="info-cell"><span>${ui.esc(t("common.duration"))}</span><strong dir="ltr">${
      metadata?.duration_seconds ? ui.formatDuration(metadata.duration_seconds) : "—"
    }</strong></div>
    <div class="info-cell"><span>${ui.esc(t("common.resolution"))}</span><strong dir="ltr">${
      stream?.width && stream?.height ? `${stream.width}×${stream.height}` : "—"
    }</strong><small>${ui.esc(stream?.codec_name || "")}</small></div>
    <div class="info-cell"><span>${ui.esc(t("common.size"))}</span><strong dir="ltr">${ui.formatBytes(
      metadata?.size_bytes,
    )}</strong></div>
    <div class="info-cell"><span>${ui.esc(t("common.audio"))}</span><strong>${ui.esc(
      metadata?.has_audio ? t("common.ready") : t("common.notAvailable"),
    )}</strong><small>${ui.esc(metadata?.audio_streams?.[0]?.codec_name || "")}</small></div>
    <div class="info-cell"><span>${ui.esc(t("common.source"))}</span><strong>${ui.esc(
      job.source_type,
    )}</strong><small title="${ui.attr(job.source_value)}">${ui.esc(
      String(job.source_value || "").slice(0, 70),
    )}</small></div>
    <div class="info-cell"><span>${ui.esc(t("common.id"))}</span><strong class="mono" dir="ltr">${ui.esc(
      job.id.slice(0, 12),
    )}</strong><small>${ui.esc(job.id)}</small></div>
  </div>`;

  const controls = `<form class="form-section" id="analysis-controls">
    <div class="form-section-header">
      <div>
        <h3>${ui.esc(t("analysis.analysisState"))}</h3>
        <p>${ui.esc(t("analysis.providerOptional"))}</p>
      </div>
      ${ui.statusPill(job.status)}
    </div>
    <div class="field-row">
      <label>${ui.esc(t("analysis.language"))}
        <select id="analysis-language">
          <option value="">${ui.esc(t("common.auto"))}</option>
          <option value="en">${ui.esc(t("common.english"))}</option>
          <option value="ar">${ui.esc(t("common.arabic"))}</option>
        </select>
      </label>
      <label>${ui.esc(t("analysis.contentType"))}
        <select id="analysis-content-type">
          ${contentTypes
            .map(
              (value) => `<option value="${value}" ${value === "auto" ? "selected" : ""}>${ui.esc(
                value === "auto" ? t("common.auto") : value,
              )}</option>`,
            )
            .join("")}
        </select>
      </label>
    </div>
    <label class="check-row">
      <input type="checkbox" id="analysis-use-llm" checked />
      <span>${ui.esc(t("analysis.useLlm"))}</span>
    </label>
    <details class="details">
      <summary>${ui.esc(t("common.provider"))} / ${ui.esc(t("common.model"))}</summary>
      <div class="details-body field-row" style="margin-block-start:10px">
        <label>${ui.esc(t("common.provider"))}<input id="analysis-provider" placeholder="auto" autocomplete="off" /></label>
        <label>${ui.esc(t("common.model"))}<input id="analysis-model" placeholder="auto" autocomplete="off" /></label>
      </div>
    </details>
    <div class="segmented" style="margin-block-start:2px">
      <button class="button button-primary" type="button" data-action="run-analysis" data-mode="transcribe">${ui.icon(
        "spark",
      )}<span>${ui.esc(transcript ? t("analysis.refreshTranscript") : t("analysis.transcribe"))}</span></button>
      <button class="button button-secondary" type="button" data-action="run-analysis" data-mode="highlights">${ui.icon(
        "spark",
      )}<span>${ui.esc(highlightResult ? t("analysis.refreshHighlights") : t("analysis.detectHighlights"))}</span></button>
      <button class="button button-secondary" type="button" data-action="run-analysis" data-mode="analyze">${ui.icon(
        "sliders",
      )}<span>${ui.esc(t("analysis.runAnalysis"))}</span></button>
    </div>
    <div id="analysis-status" class="notice" hidden aria-live="polite"></div>
  </form>`;

  const classificationHtml = classification
    ? `<div class="form-section">
        <div class="form-section-header">
          <div><h3>${ui.esc(t("analysis.classification"))}</h3><p>${ui.esc(
            t("analysis.detectedBy", { method: highlightResult.method || "deterministic" }),
          )}</p></div>
          <span class="tag is-teal">${ui.esc(classification.content_type)}</span>
        </div>
        <div class="signal-list">
          <span>${ui.esc(t("common.confidence"))} ${ui.formatConfidence(classification.confidence)}</span>
          ${classification.ambiguous ? `<span>${ui.esc(t("common.warning"))}</span>` : ""}
          ${Object.entries(classification.scores || {})
            .sort((a, b) => b[1] - a[1])
            .slice(0, 4)
            .map(([key, value]) => `<span>${ui.esc(key)} ${ui.formatConfidence(value)}</span>`)
            .join("")}
        </div>
        ${
          classification.reasons?.length
            ? `<div class="signal-list">${classification.reasons
                .map((reason) => `<span>${ui.esc(reason)}</span>`)
                .join("")}</div>`
            : `<p class="muted small">${ui.esc(t("analysis.noClassification"))}</p>`
        }
      </div>`
    : `<div class="form-section"><h3>${ui.esc(t("analysis.classification"))}</h3><p class="muted small">${ui.esc(
        t("analysis.noClassification"),
      )}</p></div>`;

  const signalsHtml = Object.keys(signals).length
    ? `<div class="form-section">
        <div class="form-section-header"><h3>${ui.esc(t("analysis.signals"))}</h3><span class="tag">${
          highlightResult?.method || "deterministic"
        }</span></div>
        <div class="signal-list">
          ${Object.entries(signals)
            .filter(([, value]) => value !== null && typeof value !== "object")
            .slice(0, 14)
            .map(
              ([key, value]) =>
                `<span>${ui.esc(key)}: ${ui.esc(
                  typeof value === "number" ? String(Math.round(value * 1000) / 1000) : value,
                )}</span>`,
            )
            .join("")}
        </div>
      </div>`
    : "";

  const transcriptSummary = `<div class="form-section">
    <div class="form-section-header">
      <div><h3>${ui.esc(t("analysis.transcriptSummary"))}</h3><p>${ui.esc(
        transcript?.language || t("common.unknown"),
      )}</p></div>
      <span class="tag ${transcript ? "is-teal" : ""}">${ui.esc(
        transcript ? t("common.ready") : t("common.queued"),
      )}</span>
    </div>
    ${
      transcript
        ? `<div class="signal-list"><span>${ui.esc(
            t("analysis.segmentCount", { count: ui.formatNumberLabel(transcriptSegments.length) }),
          )}</span><span>${ui.esc(
            t("analysis.wordCount", { count: ui.formatNumberLabel(words) }),
          )}</span><span dir="ltr">${ui.esc(ui.formatDuration(transcript.duration || 0))}</span></div>
          <p class="muted small">${ui.esc(String(transcript.text || "").slice(0, 260))}${
            String(transcript.text || "").length > 260 ? "…" : ""
          }</p>`
        : `<p class="muted small">${ui.esc(t("analysis.noTranscriptBody"))}</p>`
    }
  </div>`;

  const highlightSummary = `<div class="form-section">
    <div class="form-section-header">
      <div><h3>${ui.esc(t("analysis.highlightSummary"))}</h3><p>${ui.esc(
        highlightResult?.content_type || t("common.unknown"),
      )}</p></div>
      <span class="tag ${highlights.length ? "is-accent" : ""}">${ui.esc(
        `${ui.formatNumberLabel(highlights.length)} ${t("common.highlights")}`,
      )}</span>
    </div>
    ${
      highlights.length
        ? `<div class="signal-list">${highlights
            .slice(0, 5)
            .map(
              (highlight) =>
                `<span>${ui.esc(highlight.title)} · ${ui.esc(ui.formatDuration(highlight.end_time - highlight.start_time))}</span>`,
            )
            .join("")}</div>`
        : `<p class="muted small">${ui.esc(t("analysis.noHighlightsBody"))}</p>`
    }
  </div>`;

  const overview = `<div class="analysis-grid">
    <div class="stack">
      ${sourceInfo}
      ${transcriptSummary}
      ${highlightSummary}
    </div>
    <div class="stack">
      ${controls}
      ${classificationHtml}
      ${signalsHtml}
    </div>
  </div>`;

  const transcriptContent = transcript
    ? `<div class="transcript-tools">
        <div class="search-field" style="max-width:340px">
          ${ui.icon("search")}
          <input id="transcript-search" type="search" placeholder="${ui.attr(
            t("analysis.transcriptSearch"),
          )}" aria-label="${ui.attr(t("analysis.transcriptSearch"))}" />
        </div>
        <button class="button button-secondary button-small" type="button" data-action="copy-transcript">${ui.icon(
          "copy",
        )}<span>${ui.esc(t("analysis.copyTranscript"))}</span></button>
        <button class="button button-secondary button-small" type="button" data-action="download-transcript">${ui.icon(
          "download",
        )}<span>${ui.esc(t("analysis.downloadTranscript"))}</span></button>
      </div>
      <div class="transcript-surface" id="transcript-surface">
        <div class="segment-list" id="transcript-list">
          ${
            transcriptSegments.length
              ? transcriptSegments.map((segment) => transcriptSegment(segment, "")).join("")
              : `<div class="transcript-text">${ui.esc(transcript.text || "")}</div>`
          }
        </div>
      </div>`
    : ui.renderEmpty({
        iconName: "transcript",
        title: t("analysis.noTranscript"),
        body: t("analysis.noTranscriptBody"),
        action: `<button class="button button-primary" type="button" data-action="run-analysis" data-mode="transcribe">${ui.esc(
          t("analysis.transcribe"),
        )}</button>`,
      });

  const highlightsContent = highlights.length
    ? `<div class="section-title">
        <h2>${ui.esc(t("analysis.highlights"))} <span class="muted small">${ui.esc(
          t("analysis.highlightCount", { count: highlights.length }),
        )}</span></h2>
        <div class="page-actions">
          <span class="small muted" id="highlight-selected-count"></span>
          <button class="button button-ghost button-small" type="button" data-action="select-all-highlights">${ui.esc(
            t("common.selectAll"),
          )}</button>
          <button class="button button-ghost button-small" type="button" data-action="clear-highlights">${ui.esc(
            t("common.clear"),
          )}</button>
          <a class="button button-primary button-small" id="highlight-export-link" href="#/video/${ui.attr(
            job.id,
          )}/export">${ui.icon("clips")}<span>${ui.esc(t("common.exportSelected"))}</span></a>
        </div>
      </div>
      <div class="highlight-list" id="highlight-list">
        ${highlights.map((highlight, index) => highlightCard(highlight, index, selected.has(index))).join("")}
      </div>
      <div class="notice" style="margin-block-start:14px">${ui.icon("info")}<div>${ui.esc(
        t("analysis.customTrimNote"),
      )}</div></div>`
    : ui.renderEmpty({
        iconName: "spark",
        title: t("analysis.noHighlights"),
        body: t("analysis.noHighlightsBody"),
        action: `<button class="button button-primary" type="button" data-action="run-analysis" data-mode="highlights">${ui.esc(
          t("analysis.detectHighlights"),
        )}</button>`,
      });

  const clipsContent = clips.length
    ? `<div class="section-title">
        <h2>${ui.esc(t("common.clips"))} <span class="muted small">${ui.esc(
          t("analysis.clipCount", { count: clips.length }),
        )}</span></h2>
        <a class="button button-primary button-small" href="#/video/${ui.attr(job.id)}/export">${ui.icon(
          "clips",
        )}<span>${ui.esc(t("analysis.exportClips"))}</span></a>
      </div>
      <div class="clip-grid">${clips.map(clipCard).join("")}</div>`
    : ui.renderEmpty({
        iconName: "clips",
        title: t("analysis.noClips"),
        body: t("analysis.noClipsBody"),
        action: highlights.length
          ? `<a class="button button-primary" href="#/video/${ui.attr(job.id)}/export">${ui.esc(
              t("analysis.exportClips"),
            )}</a>`
          : "",
      });

  const tabContent =
    tab === "transcript" ? transcriptContent : tab === "highlights" ? highlightsContent : tab === "clips" ? clipsContent : overview;

  const html = `<div class="page-enter">
    ${header}
    ${pipeline}
    ${tabsHtml}
    <div id="analysis-tab-content">${tabContent}</div>
  </div>`;

  return {
    title: ui.jobTitle(job),
    html,
    mount() {
      const exportLink = main.querySelector("#highlight-export-link");
      const updateSelection = () => {
        const count = selected.size;
        const countHolder = main.querySelector("#highlight-selected-count");
        if (countHolder) {
          countHolder.textContent = t("analysis.selectedCount", { count });
        }
        if (exportLink) {
          exportLink.classList.toggle("is-disabled", count === 0);
          exportLink.setAttribute("aria-disabled", String(count === 0));
          exportLink.href = count ? `#/video/${job.id}/export` : "#";
        }
      };
      updateSelection();

      main.querySelector("#highlight-list")?.addEventListener("change", (event) => {
        const checkbox = event.target.closest("[data-highlight-checkbox]");
        if (!checkbox) return;
        const index = Number(checkbox.dataset.highlightCheckbox);
        if (checkbox.checked) selected.add(index);
        else selected.delete(index);
        checkbox.closest(".highlight-card")?.classList.toggle("is-selected", checkbox.checked);
        updateSelection();
      });

      main.querySelector("[data-action='select-all-highlights']")?.addEventListener("click", () => {
        highlights.forEach((_, index) => selected.add(index));
        main.querySelectorAll("[data-highlight-checkbox]").forEach((checkbox) => {
          checkbox.checked = true;
          checkbox.closest(".highlight-card")?.classList.add("is-selected");
        });
        updateSelection();
      });

      main.querySelector("[data-action='clear-highlights']")?.addEventListener("click", () => {
        selected.clear();
        main.querySelectorAll("[data-highlight-checkbox]").forEach((checkbox) => {
          checkbox.checked = false;
          checkbox.closest(".highlight-card")?.classList.remove("is-selected");
        });
        updateSelection();
      });

      const transcriptSearch = main.querySelector("#transcript-search");
      transcriptSearch?.addEventListener("input", () => {
        const query = transcriptSearch.value.trim();
        const list = main.querySelector("#transcript-list");
        if (!list) return;
        if (!transcriptSegments.length) return;
        const filtered = transcriptSegments.filter((segment) =>
          String(segment.text || "").toLowerCase().includes(query.toLowerCase()),
        );
        list.innerHTML = filtered.length
          ? filtered.map((segment) => transcriptSegment(segment, query)).join("")
          : `<div class="transcript-text muted">${ui.esc(t("analysis.transcriptEmptySearch"))}</div>`;
      });

      main.querySelector("[data-action='copy-transcript']")?.addEventListener("click", async () => {
        try {
          await ui.copyText(transcript?.text || "");
          ui.toast(t("analysis.transcriptCopied"), "success");
        } catch (error) {
          ui.toast(error.message || t("common.error"), "error");
        }
      });

      main.querySelector("[data-action='download-transcript']")?.addEventListener("click", () => {
        ui.downloadText(`transcript-${job.id.slice(0, 8)}.txt`, transcript?.text || "");
        ui.toast(t("analysis.transcriptDownloaded"), "success");
      });

      main.querySelectorAll("[data-action='run-analysis']").forEach((button) => {
        button.addEventListener("click", async () => {
          await runAnalysis(job, button, transcript, highlights, () => renderRoute());
        });
      });

      const wasActive = isJobActive(job);
      startPolling(
        async ({ signal }) => {
          const next = await api.getVideoJob(job.id, { signal });
          if (isJobActive(next) || wasActive) renderRoute();
        },
        { interval: 4000, token, isActive: () => wasActive },
      );
    },
  };
}

function transcriptSegment(segment, query) {
  const text = String(segment.text || "");
  let body = ui.esc(text);
  if (query) {
    const pattern = new RegExp(`(${ui.escapeRegExp(query)})`, "gi");
    body = text
      .split(pattern)
      .map((part, index) =>
        index % 2 === 1 ? `<mark>${ui.esc(part)}</mark>` : ui.esc(part),
      )
      .join("");
  }
  return `<div class="segment">
    <span class="segment-time" dir="ltr">${ui.esc(ui.formatClock(segment.start, true))}<br />${ui.esc(
      ui.formatClock(segment.end, true),
    )}</span>
    <p>${body}</p>
  </div>`;
}

function highlightCard(highlight, index, isSelected) {
  const confidence = Math.round((Number(highlight.confidence) || 0) * 100);
  const excerpt = highlightExcerpt(highlight);
  return `<article class="highlight-card ${isSelected ? "is-selected" : ""}" data-highlight-card="${index}">
    <input type="checkbox" data-highlight-checkbox="${index}" ${
      isSelected ? "checked" : ""
    } aria-label="${ui.attr(highlight.title || `${t("common.clip")} ${index + 1}`)}" />
    <div>
      <div class="highlight-top">
        <div class="highlight-title">
          <span class="highlight-index">${String(index + 1).padStart(2, "0")}</span>
          <div>
            <h3>${ui.esc(highlight.title)}</h3>
            <div class="highlight-time">
              <span dir="ltr">${ui.esc(ui.formatClock(highlight.start_time, true))} → ${ui.esc(
                ui.formatClock(highlight.end_time, true),
              )}</span>
              <span>${ui.esc(ui.formatDuration(highlight.end_time - highlight.start_time))}</span>
              <span>${ui.esc(highlight.category || "")}</span>
            </div>
          </div>
        </div>
        <div class="highlight-confidence">
          <span>${ui.esc(t("common.confidence"))}</span>
          <div class="confidence-meter"><i style="width:${confidence}%"></i></div>
          <strong>${ui.formatConfidence(highlight.confidence)}</strong>
        </div>
      </div>
      ${highlight.description ? `<p class="highlight-description">${ui.esc(highlight.description)}</p>` : ""}
      ${excerpt ? `<p class="highlight-excerpt">${ui.esc(excerpt)}</p>` : ""}
      <div class="highlight-footer">
        <span class="tag is-teal">${ui.esc(highlight.source || "deterministic")}</span>
        ${highlight.selection_reason ? `<span>${ui.esc(highlight.selection_reason)}</span>` : ""}
        ${
          highlight.signal_score !== null && highlight.signal_score !== undefined
            ? `<span>${ui.esc(t("common.signals"))} ${ui.formatConfidence(highlight.signal_score)}</span>`
            : ""
        }
      </div>
    </div>
  </article>`;
}

async function runAnalysis(job, button, transcript, highlights, done) {
  const form = main.querySelector("#analysis-controls");
  const status = main.querySelector("#analysis-status");
  const mode = button.dataset.mode;
  const language = form?.querySelector("#analysis-language")?.value || null;
  const contentType = form?.querySelector("#analysis-content-type")?.value || "auto";
  const useLlm = Boolean(form?.querySelector("#analysis-use-llm")?.checked);
  const provider = form?.querySelector("#analysis-provider")?.value.trim() || null;
  const model = form?.querySelector("#analysis-model")?.value.trim() || null;
  const payload = {
    provider,
    model,
    language,
    refresh: Boolean(transcript || highlights.length),
  };
  if (mode !== "transcribe") {
    payload.content_type = contentType;
    payload.use_llm = useLlm;
  }
  const labels = {
    transcribe: t("analysis.transcribing"),
    highlights: t("analysis.highlighting"),
    analyze: t("analysis.analyzing"),
  };
  const success = {
    transcribe: t("analysis.transcriptDone"),
    highlights: t("analysis.highlightsDone"),
    analyze: t("analysis.analysisDone"),
  };
  const failure = {
    transcribe: t("analysis.transcriptFailed"),
    highlights: t("analysis.highlightsFailed"),
    analyze: t("analysis.analysisFailed"),
  };
  ui.setBusy(button, true, labels[mode]);
  if (status) {
    status.hidden = false;
    status.className = "notice";
    status.innerHTML = `${ui.icon("spark", "spin")}<div>${ui.esc(labels[mode])}</div>`;
  }
  try {
    if (mode === "transcribe") await api.transcribeVideo(job.id, payload);
    else if (mode === "highlights") await api.highlightsVideo(job.id, payload);
    else await api.analyzeVideo(job.id, payload);
    ui.toast(success[mode], "success");
    done();
  } catch (error) {
    ui.setBusy(button, false);
    if (status) {
      status.hidden = false;
      status.className = "notice is-error";
      status.innerHTML = `${ui.icon("alert")}<div><strong>${ui.esc(
        failure[mode],
      )}</strong> ${ui.esc(error.message || t("error.genericBody"))}</div>`;
    }
    ui.toast(error.message || failure[mode], "error");
  }
}

async function exportView(route, token) {
  const jobId = route.id;
  const [job, highlightsPayload, config] = await Promise.all([
    api.getVideoJob(jobId),
    api.getHighlights(jobId),
    api.clipsConfig(),
  ]);
  state.videoCache.set(job.id, job);
  const highlightResult = highlightsPayload.highlight_result || job.highlight_result || null;
  const highlights = highlightResult?.highlights || [];
  const selected = selectionFor(job.id, highlights.length);
  const hasTranscript = Boolean(job.transcript);
  if (!highlights.length) {
    return {
      title: t("export.title"),
      html: `<div class="page-enter">
        ${pageHead({
          context: t("export.context"),
          title: t("export.title"),
          description: t("export.description"),
          actions: `<a class="button button-secondary" href="#/video/${ui.attr(job.id)}">${ui.esc(
            t("export.backToAnalysis"),
          )}</a>`,
        })}
        ${ui.renderEmpty({
          iconName: "spark",
          title: t("export.noHighlightsTitle"),
          body: t("export.noHighlightsBody"),
          action: `<a class="button button-primary" href="#/video/${ui.attr(
            job.id,
          )}?tab=highlights">${ui.esc(t("analysis.detectHighlights"))}</a>`,
        })}
      </div>`,
    };
  }
  const formats = Object.keys(config.formats || { vertical: {}, square: {}, landscape: {} });
  const qualities = Object.keys(config.qualities || { draft: {}, standard: {}, high: {} });
  const defaultFormats = config.default_formats?.length ? config.default_formats : ["vertical"];
  const defaultQuality = config.default_quality || "standard";
  const defaultFit = config.default_fit || "crop";
  const fits = ["crop", "pad", "blur"];

  const formatOptions = formats
    .map((format) => {
      const profile = config.formats?.[format] || {};
      return `<label class="format-option">
        <input type="checkbox" name="format" value="${ui.attr(format)}" ${
          defaultFormats.includes(format) ? "checked" : ""
        } />
        <span class="format-diagram"><i data-format="${ui.attr(format)}"></i></span>
        <strong>${ui.esc(formatName(format))}</strong>
        <small>${ui.esc(profile.width && profile.height ? `${profile.width}×${profile.height}` : "")}</small>
      </label>`;
    })
    .join("");

  const qualityOptions = qualities
    .map(
      (quality) => `<label class="fit-option">
        <input type="radio" name="quality" value="${ui.attr(quality)}" ${
          quality === defaultQuality ? "checked" : ""
        } />
        <span class="fit-icon">${ui.icon(quality === "high" ? "spark" : quality === "draft" ? "clock" : "sliders")}</span>
        <span><strong>${ui.esc(qualityName(quality))}</strong><small>${ui.esc(
          t(`export.quality${quality[0].toUpperCase() + quality.slice(1)}Hint`),
        )}</small></span>
      </label>`,
    )
    .join("");

  const fitOptions = fits
    .map(
      (fit) => `<label class="fit-option">
        <input type="radio" name="fit" value="${ui.attr(fit)}" ${fit === defaultFit ? "checked" : ""} />
        <span class="fit-icon">${ui.icon(fit === "crop" ? "film" : fit === "blur" ? "spark" : "sliders")}</span>
        <span><strong>${ui.esc(fitName(fit))}</strong><small>${ui.esc(
          t(`export.fit${fit[0].toUpperCase() + fit.slice(1)}Hint`),
        )}</small></span>
      </label>`,
    )
    .join("");

  const selectedHighlights = highlights
    .map((highlight, index) => ({ highlight, index }))
    .filter(({ index }) => selected.has(index));
  const initialActiveIndex = selectedHighlights[0]?.index ?? null;

  const html = `<div class="page-enter">
    ${pageHead({
      context: t("export.context"),
      title: t("export.title"),
      description: t("export.description"),
      actions: `<a class="button button-secondary" href="#/video/${ui.attr(job.id)}?tab=highlights">${ui.icon(
        "arrow",
      )}<span>${ui.esc(t("export.backToAnalysis"))}</span></a>`,
    })}
    <form class="export-layout" id="export-form" data-job-id="${ui.attr(job.id)}">
      <div class="export-form">
        <section class="form-section">
          <div class="form-section-header">
            <div><h3>${ui.esc(t("export.selectHighlights"))}</h3><p>${ui.esc(
              t("analysis.customTrimNote"),
            )}</p></div>
            <span class="tag is-accent">${ui.esc(
              t("analysis.highlightCount", { count: highlights.length }),
            )}</span>
          </div>
          <div class="selected-highlights">
            ${highlights
              .map((highlight, index) =>
                exportHighlightRow(highlight, index, {
                  checked: selected.has(index),
                  active: initialActiveIndex === index,
                }),
              )
              .join("")}
          </div>
        </section>
        <section class="form-section trim-editor" id="trim-editor" aria-labelledby="trim-editor-title"></section>
        <section class="form-section">
          <div class="form-section-header">
            <div><h3>${ui.esc(t("export.formats"))}</h3><p>${ui.esc(t("export.formatsHint"))}</p></div>
          </div>
          <div class="format-grid">${formatOptions}</div>
        </section>
        <section class="form-section">
          <div class="form-section-header">
            <div><h3>${ui.esc(t("export.quality"))}</h3><p>${ui.esc(t("export.localOnlyNote"))}</p></div>
          </div>
          <div class="fit-options">${qualityOptions}</div>
        </section>
        <section class="form-section">
          <div class="form-section-header">
            <div><h3>${ui.esc(t("export.fit"))}</h3><p>${ui.esc(t("export.formatsHint"))}</p></div>
          </div>
          <div class="fit-options">${fitOptions}</div>
        </section>
        <section class="form-section">
          <div class="form-section-header">
            <div><h3>${ui.esc(t("export.captions"))}</h3><p>${ui.esc(t("export.captionsHint"))}</p></div>
          </div>
          <label class="check-row">
            <input type="checkbox" id="captions-toggle" ${hasTranscript ? "" : "disabled"} />
            <span>${ui.esc(t("export.captions"))}<small>${
              hasTranscript ? ui.esc(t("analysis.clipBoundaryNote")) : ui.esc(t("export.captionsNoTranscript"))
            }</small></span>
          </label>
          <label>${ui.esc(t("export.captionLanguage"))}
            <select id="caption-language" ${hasTranscript ? "" : "disabled"}>
              <option value="">${ui.esc(t("export.captionLanguageAuto"))}</option>
              <option value="en">${ui.esc(t("common.english"))}</option>
              <option value="ar">${ui.esc(t("common.arabic"))}</option>
            </select>
          </label>
        </section>
        <section class="form-section">
          <div class="form-section-header">
            <div><h3>${ui.esc(t("export.visual"))}</h3><p>${ui.esc(t("export.visualHint"))}</p></div>
          </div>
          <label class="check-row">
            <input type="checkbox" id="visual-enhancement" checked />
            <span>${ui.esc(t("export.visualEnhancement"))}<small>${ui.esc(t("export.visualEnhancementHint"))}</small></span>
          </label>
          <label>${ui.esc(t("export.visualIntensity"))}
            <select id="visual-intensity">
              <option value="subtle">${ui.esc(t("export.intensitySubtle"))}</option>
              <option value="balanced" selected>${ui.esc(t("export.intensityBalanced"))}</option>
              <option value="strong">${ui.esc(t("export.intensityStrong"))}</option>
            </select>
          </label>
          <div class="check-row">
            <label><input type="checkbox" id="visual-zoom" checked /> ${ui.esc(t("export.visualZoom"))}</label>
            <label><input type="checkbox" id="visual-punch" checked /> ${ui.esc(t("export.visualPunch"))}</label>
            <label><input type="checkbox" id="visual-emphasis" checked /> ${ui.esc(t("export.visualEmphasis"))}</label>
            <label><input type="checkbox" id="visual-transitions" checked /> ${ui.esc(t("export.visualTransitions"))}</label>
            <label><input type="checkbox" id="visual-callouts" checked /> ${ui.esc(t("export.visualCallouts"))}</label>
          </div>
        </section>
        <section class="form-section">
          <div class="form-section-header">
            <div><h3>${ui.esc(t("export.renderMode"))}</h3><p>${ui.esc(t("export.renderNowHint"))}</p></div>
          </div>
          <div class="segmented">
            <input type="radio" id="render-queue" name="render-mode" value="queue" checked />
            <label for="render-queue">${ui.esc(t("export.queueRender"))}</label>
            <input type="radio" id="render-now" name="render-mode" value="now" />
            <label for="render-now">${ui.esc(t("common.renderNow"))}</label>
          </div>
          <p class="field-hint">${ui.esc(
            t("export.estimates", {
              min: config.min_clip_seconds,
              max: config.max_clip_seconds,
              formats: formats.length,
            }),
          )}</p>
        </section>
      </div>
      <aside class="summary-sticky">
        <section class="form-section">
          <div class="form-section-header">
            <div><h3>${ui.esc(t("export.summary"))}</h3><p>${ui.esc(t("export.queueRenderHint"))}</p></div>
          </div>
          <dl class="summary-list">
            <div class="summary-row"><dt>${ui.esc(t("export.selectedCount"))}</dt><dd id="summary-selected">${
              selectedHighlights.length
            }</dd></div>
            <div class="summary-row"><dt>${ui.esc(t("export.outputCount"))}</dt><dd id="summary-output">${
              selectedHighlights.length * defaultFormats.length
            }</dd></div>
            <div class="summary-row"><dt>${ui.esc(t("export.captionState"))}</dt><dd id="summary-captions">${ui.esc(
              t("export.captionsOff"),
            )}</dd></div>
            <div class="summary-row"><dt>${ui.esc(t("common.quality"))}</dt><dd id="summary-quality">${ui.esc(
              qualityName(defaultQuality),
            )}</dd></div>
            <div class="summary-row"><dt>${ui.esc(t("common.fit"))}</dt><dd id="summary-fit">${ui.esc(
              fitName(defaultFit),
            )}</dd></div>
          </dl>
          <button class="button button-primary button-block" type="button" data-action="create-clips" data-job-id="${ui.attr(
            job.id,
          )}">${ui.icon("clips")}<span>${ui.esc(t("common.createClips"))}</span></button>
          <div class="notice">${ui.icon("info")}<div>${ui.esc(t("analysis.customTrimNote"))}</div></div>
        </section>
        <section class="form-section">
          <h3>${ui.esc(t("export.selectHighlights"))}</h3>
          <div class="selected-highlights" id="export-selected-list">
            ${selectedHighlights
              .map(({ highlight, index }) =>
                exportHighlightRow(highlight, index, {
                  summary: true,
                  active: initialActiveIndex === index,
                }),
              )
              .join("")}
          </div>
        </section>
      </aside>
    </form>
  </div>`;

  return {
    title: t("export.title"),
    html,
    mount() {
      const form = main.querySelector("#export-form");
      if (!form) return;
      let editor = null;
      const selectedInputs = () => [
        ...form.querySelectorAll('input[name="highlight"]:checked'),
      ];
      const update = () => {
        const checkedInputs = selectedInputs();
        const formatInputs = [...form.querySelectorAll('input[name="format"]:checked')];
        const quality = form.querySelector('input[name="quality"]:checked')?.value || defaultQuality;
        const fit = form.querySelector('input[name="fit"]:checked')?.value || defaultFit;
        const captions = Boolean(form.querySelector("#captions-toggle")?.checked);
        const count = checkedInputs.length;
        const outputs = count * formatInputs.length;
        const set = (id, value) => {
          const element = main.querySelector(id);
          if (element) element.textContent = value;
        };
        set("#summary-selected", count);
        set("#summary-output", outputs);
        set("#summary-captions", captions ? t("export.captionsOn") : t("export.captionsOff"));
        set("#summary-quality", qualityName(quality));
        set("#summary-fit", fitName(fit));
        selected.clear();
        checkedInputs.forEach((input) => selected.add(Number(input.value)));
        const activeIndex = editor?.getActiveIndex() ?? null;
        form.querySelectorAll("[data-trim-row]").forEach((row) => {
          const index = Number(row.dataset.trimRow);
          const highlight = highlights[index];
          const range = editor?.rangeFor(index);
          const custom = editor?.isCustom(index) ?? false;
          row.classList.toggle("is-active", activeIndex === index);
          row.classList.toggle("is-custom", custom);
          const duration = row.querySelector(".duration");
          if (duration) {
            const start = Number(range?.start ?? highlight.start_time ?? 0);
            const end = Number(range?.end ?? highlight.end_time ?? start);
            duration.textContent = ui.formatDuration(end - start);
          }
          row
            .querySelector("[data-trim-highlight]")
            ?.setAttribute("aria-pressed", String(activeIndex === index));
        });
        const list = main.querySelector("#export-selected-list");
        if (list) {
          list.innerHTML = checkedInputs
            .map((input) => {
              const index = Number(input.value);
              return exportHighlightRow(highlights[index], index, {
                summary: true,
                active: activeIndex === index,
                range: editor?.rangeFor(index),
                custom: editor?.isCustom(index) ?? false,
              });
            })
            .join("");
        }
      };
      form.addEventListener("change", (event) => {
        const summary = event.target.closest("[data-summary-highlight]");
        let changedIndex = null;
        let changedChecked = null;
        if (summary) {
          changedIndex = Number(summary.dataset.summaryHighlight);
          changedChecked = summary.checked;
          const source = form.querySelector(
            `input[name="highlight"][value="${changedIndex}"]`,
          );
          if (source) source.checked = changedChecked;
        } else if (event.target.matches('input[name="highlight"]')) {
          changedIndex = Number(event.target.value);
          changedChecked = event.target.checked;
        }
        update();
        if (changedIndex === null || !editor) return;
        if (changedChecked) {
          editor.open(changedIndex);
        } else if (editor.getActiveIndex() === changedIndex) {
          const next = selectedInputs()[0];
          editor.open(next ? Number(next.value) : null);
        }
      });
      form.addEventListener("click", (event) => {
        const button = event.target.closest("[data-trim-highlight]");
        if (!button || !form.contains(button)) return;
        editor?.open(Number(button.dataset.trimHighlight));
        const section = main.querySelector("#trim-editor");
        const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
        section?.scrollIntoView({
          behavior: reduceMotion ? "auto" : "smooth",
          block: "nearest",
        });
      });
      editor = createTrimEditor({
        root: main.querySelector("#trim-editor"),
        highlights,
        sourceUrl: api.sourceMediaUrl(job.id),
        sourceDuration: job.metadata?.duration_seconds ?? null,
        minDuration: config.min_clip_seconds,
        maxDuration: config.max_clip_seconds,
        initialIndex: initialActiveIndex,
        onChange: update,
        onActiveChange: update,
      });
      state.trimEditor = editor;
      state.cleanup.push(() => {
        editor.destroy();
        if (state.trimEditor === editor) state.trimEditor = null;
      });
      update();
    },
  };
}

async function clipsView(token) {
  const data = await api.listClipJobs({ limit: 200 });
  let clips = data.jobs || [];
  await loadSourceJobs(clips);
  let statusFilter = "all";
  let formatFilter = "all";
  let publishFilter = "all";
  let searchValue = "";
  const selected = new Set();

  const html = `<div class="page-enter">
    ${pageHead({
      context: t("export.context"),
      title: t("clips.title"),
      description: t("clips.description"),
      actions: `<a class="button button-secondary" href="#/library">${ui.icon("library")}<span>${ui.esc(
        t("nav.library"),
      )}</span></a>
      <a class="button button-secondary" href="#/trash">${ui.icon("trash")}<span>${ui.esc(
        t("nav.trash"),
      )}</span></a>`,
    })}
    <div class="toolbar toolbar-wrap">
      <div class="search-field">
        ${ui.icon("search")}
        <input id="clips-search" type="search" autocomplete="off" placeholder="${ui.attr(
          t("clips.searchPlaceholder"),
        )}" aria-label="${ui.attr(t("common.search"))}" />
      </div>
      <div class="toolbar-actions">
        <button class="button button-ghost button-small" type="button" id="clips-select-all">${ui.icon(
          "check",
        )}<span>${ui.esc(t("common.selectAll"))}</span></button>
        <button class="button button-ghost button-small" type="button" id="clips-clear">${ui.icon(
          "cancel",
        )}<span>${ui.esc(t("common.clear"))}</span></button>
        <button class="button button-danger button-small" type="button" id="clips-delete-selected" disabled>${ui.icon(
          "trash",
        )}<span id="clips-delete-label">${ui.esc(t("common.delete"))}</span></button>
      </div>
      <span class="small muted" id="clips-count"></span>
    </div>
    <div class="filter-row" id="clip-status-filters" role="group" aria-label="${ui.attr(
      t("common.status"),
    )}"></div>
    <div class="filter-row" id="clip-format-filters" role="group" aria-label="${ui.attr(
      t("common.format"),
    )}" style="margin-block-start:8px"></div>
    <div class="filter-row" id="clip-publish-filters" role="group" aria-label="${ui.attr(
      t("clips.publishing"),
    )}" style="margin-block-start:8px"></div>
    <div id="clip-results" style="margin-block-start:18px" aria-live="polite"></div>
  </div>`;

  function filtered() {
    const query = searchValue.trim().toLowerCase();
    return clips.filter((clip) => {
      if (statusFilter !== "all" && clip.status !== statusFilter) return false;
      if (formatFilter !== "all" && clip.format !== formatFilter) return false;
      if (publishFilter === "published" && !clip.published) return false;
      if (publishFilter === "unpublished" && clip.published) return false;
      if (!query) return true;
      const source = sourceJobFor(clip);
      return [
        clip.id,
        clip.source_job_id,
        clip.format,
        clip.quality,
        clip.title,
        clip.description,
        ...(clip.hashtags || []),
        clipTitle(clip, source),
        source ? ui.jobTitle(source) : "",
      ]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query));
    });
  }

  function paintFilters() {
    const statusHolder = main.querySelector("#clip-status-filters");
    const formatHolder = main.querySelector("#clip-format-filters");
    const publishHolder = main.querySelector("#clip-publish-filters");
    if (statusHolder) {
      const counts = { all: clips.length };
      for (const status of ["ready", "processing", "queued", "failed", "cancelled"]) {
        counts[status] = clips.filter((clip) => clip.status === status).length;
      }
      statusHolder.innerHTML = Object.entries(counts)
        .map(
          ([status, count]) => `<button class="filter-chip ${statusFilter === status ? "is-active" : ""}" type="button" data-status-filter="${status}" aria-pressed="${
            statusFilter === status
          }">${ui.esc(status === "all" ? t("common.all") : ui.statusLabel(status))}<small>${ui.formatNumberLabel(
            count,
          )}</small></button>`,
        )
        .join("");
    }
    if (formatHolder) {
      const formats = ["all", "vertical", "square", "landscape"];
      formatHolder.innerHTML = formats
        .map(
          (format) => `<button class="filter-chip ${formatFilter === format ? "is-active" : ""}" type="button" data-format-filter="${format}" aria-pressed="${
            formatFilter === format
          }">${ui.esc(format === "all" ? t("common.all") : formatName(format))}</button>`,
        )
        .join("");
    }
    if (publishHolder) {
      const items = [
        ["all", t("common.all")],
        ["published", t("clips.published")],
        ["unpublished", t("clips.unpublished")],
      ];
      publishHolder.innerHTML = items
        .map(
          ([value, label]) => `<button class="filter-chip ${publishFilter === value ? "is-active" : ""}" type="button" data-publish-filter="${value}" aria-pressed="${
            publishFilter === value
          }">${ui.esc(label)}</button>`,
        )
        .join("");
    }
  }

  function paintSelection() {
    const ids = new Set(clips.map((clip) => clip.id));
    for (const id of [...selected]) if (!ids.has(id)) selected.delete(id);
    const button = main.querySelector("#clips-delete-selected");
    const label = main.querySelector("#clips-delete-label");
    if (button) button.disabled = selected.size === 0;
    if (label) {
      label.textContent = selected.size
        ? t("common.deleteCount", { count: selected.size })
        : t("common.delete");
    }
  }

  function paint() {
    const holder = main.querySelector("#clip-results");
    const countHolder = main.querySelector("#clips-count");
    if (!holder) return;
    const rows = filtered();
    if (countHolder) {
      countHolder.textContent = `${ui.formatNumberLabel(rows.length)} / ${ui.formatNumberLabel(
        clips.length,
      )}`;
    }
    if (!clips.length) {
      holder.innerHTML = ui.renderEmpty({
        iconName: "clips",
        title: t("clips.emptyTitle"),
        body: t("clips.emptyBody"),
        action: `<a class="button button-primary" href="#/library">${ui.esc(t("nav.library"))}</a>`,
      });
    } else if (!rows.length) {
      holder.innerHTML = ui.renderEmpty({
        iconName: "search",
        title: t("clips.filteredEmpty"),
        body: t("clips.searchPlaceholder"),
      });
    } else {
      holder.innerHTML = `<div class="clip-grid">${rows
        .map((clip) => clipCard(clip, { selectable: true, selected: selected.has(clip.id) }))
        .join("")}</div>`;
    }
    paintFilters();
    paintSelection();
  }

  async function deleteSelected() {
    if (!selected.size) return;
    const confirmed = await ui.confirmDialog(
      t("clips.bulkDeleteConfirm", { count: selected.size }),
      {
        title: t("clips.bulkDelete"),
        confirmLabel: t("common.delete"),
        danger: true,
      },
    );
    if (!confirmed) return;
    const ids = [...selected];
    const results = await Promise.allSettled(ids.map((id) => api.deleteClip(id)));
    const failures = results.filter((result) => result.status === "rejected").length;
    if (failures) {
      ui.toast(t("clips.bulkDeletePartial", { count: failures }), "warning");
    } else {
      ui.toast(t("clips.movedToTrash", { count: ids.length }), "success");
    }
    renderRoute();
  }

  return {
    title: t("clips.title"),
    html,
    mount() {
      paint();
      const search = main.querySelector("#clips-search");
      search?.addEventListener("input", () => {
        searchValue = search.value;
        paint();
      });
      main.querySelector("#clip-status-filters")?.addEventListener("click", (event) => {
        const chip = event.target.closest("[data-status-filter]");
        if (!chip) return;
        statusFilter = chip.dataset.statusFilter;
        paint();
      });
      main.querySelector("#clip-format-filters")?.addEventListener("click", (event) => {
        const chip = event.target.closest("[data-format-filter]");
        if (!chip) return;
        formatFilter = chip.dataset.formatFilter;
        paint();
      });
      main.querySelector("#clip-publish-filters")?.addEventListener("click", (event) => {
        const chip = event.target.closest("[data-publish-filter]");
        if (!chip) return;
        publishFilter = chip.dataset.publishFilter;
        paint();
      });
      main.querySelector("#clips-select-all")?.addEventListener("click", () => {
        filtered().forEach((clip) => selected.add(clip.id));
        paint();
      });
      main.querySelector("#clips-clear")?.addEventListener("click", () => {
        selected.clear();
        paint();
      });
      main.querySelector("#clips-delete-selected")?.addEventListener("click", deleteSelected);
      main.querySelector("#clip-results")?.addEventListener("change", (event) => {
        const checkbox = event.target.closest("[data-select-clip]");
        if (!checkbox) return;
        if (checkbox.checked) selected.add(checkbox.dataset.selectClip);
        else selected.delete(checkbox.dataset.selectClip);
        paintSelection();
      });
      startPolling(
        async ({ signal, force }) => {
          const activeIds = clips.filter(isClipActive).map((clip) => clip.id);
          if (!activeIds.length && !force) return;
          const next = await api.listClipJobs(
            force
              ? { limit: 200 }
              : { ids: activeIds.join(","), limit: Math.min(activeIds.length, 500) },
            { signal },
          );
          const updated = next.jobs || [];
          let nextClips = updated;
          if (!force) {
            const byId = new Map(updated.map((clip) => [clip.id, clip]));
            const polled = new Set(activeIds);
            const complete = activeIds.length <= 500;
            nextClips = clips
              .filter((clip) => !complete || !polled.has(clip.id) || byId.has(clip.id))
              .map((clip) => byId.get(clip.id) || clip);
          }
          if (sameJson(nextClips, clips)) return;
          clips = nextClips;
          await loadSourceJobs(clips, signal);
          paint();
        },
        { interval: 4000, token, isActive: () => clips.some(isClipActive) },
      );
    },
  };
}

async function clipDetailView(route, token) {
  const clip = await api.getClipJob(route.id);
  let source = state.videoCache.get(clip.source_job_id) || null;
  if (!source) {
    try {
      source = await api.getVideoJob(clip.source_job_id);
      state.videoCache.set(clip.source_job_id, source);
    } catch {
      source = null;
    }
  }
  const ready = clip.status === "ready";
  const title = clipTitle(clip, source);
  const downloadUrl = api.clipDownloadUrl(clip.id);
  const hashtagsText = (clip.hashtags || []).join(" ");
  const player = ready
    ? `<video class="clip-detail-player" src="${downloadUrl}" controls playsinline preload="metadata"></video>`
    : `<div class="clip-detail-player is-placeholder">${ui.icon("film")}<p>${ui.esc(
        t("clips.noPreview"),
      )}</p></div>`;
  const metadataStatus = clip.metadata_status
    ? metadataStatusLabel(clip.metadata_status)
    : t("clips.metadataNone");
  const errorNotice = clip.error?.message
    ? `<div class="notice is-error">${ui.icon("alert")}<div>${ui.esc(clip.error.message)}</div></div>`
    : "";
  const metadataError = clip.metadata_error
    ? `<div class="notice is-warning">${ui.icon("alert")}<div>${ui.esc(clip.metadata_error)}</div></div>`
    : "";

  const html = `<div class="page-enter">
    ${pageHead({
      context: t("clips.detailContext"),
      title,
      description: t("clips.detailDescription"),
      actions: `<a class="button button-secondary" href="#/clips">${ui.icon("arrow")}<span>${ui.esc(
        t("clips.backToClips"),
      )}</span></a>
      ${
        ready
          ? `<a class="button button-primary" href="${downloadUrl}" download>${ui.icon(
              "download",
            )}<span>${ui.esc(t("common.download"))}</span></a>`
          : ""
      }
      <button class="button button-danger" type="button" data-action="delete-clip" data-clip-id="${ui.attr(
        clip.id,
      )}">${ui.icon("trash")}<span>${ui.esc(t("common.delete"))}</span></button>`,
    })}
    <div class="clip-detail-layout">
      <div class="stack">
        <section class="form-section clip-player-card">
          ${player}
          <div class="signal-list compact">
            ${ui.statusPill(clip.status)}
            ${clipPublishBadge(clip)}
            <span class="tag">${ui.esc(metadataStatus)}</span>
          </div>
          <div class="info-grid">
            <div class="info-cell"><span>${ui.esc(t("common.duration"))}</span><strong dir="ltr">${ui.esc(
              ui.formatDuration(clip.duration),
            )}</strong></div>
            <div class="info-cell"><span>${ui.esc(t("common.format"))}</span><strong>${ui.esc(
              `${formatName(clip.format)} · ${clip.width}×${clip.height}`,
            )}</strong></div>
            <div class="info-cell"><span>${ui.esc(t("common.created"))}</span><strong>${ui.esc(
              ui.formatDateTimeLabel(clip.created_at),
            )}</strong></div>
            <div class="info-cell"><span>${ui.esc(t("common.size"))}</span><strong dir="ltr">${ui.esc(
              ui.formatBytes(clip.size_bytes),
            )}</strong></div>
          </div>
          ${errorNotice}
        </section>
        <section class="form-section">
          <div class="form-section-header">
            <div><h3>${ui.esc(t("clips.publishing"))}</h3><p>${ui.esc(t("clips.publishingHint"))}</p></div>
          </div>
          <label class="check-row">
            <input type="checkbox" id="clip-published" ${clip.published ? "checked" : ""} ${
              ready ? "" : "disabled"
            }/>
            <span>${ui.esc(t("clips.markPublished"))}<small id="clip-published-state">${ui.esc(
              clip.published ? t("clips.publishedAt", { time: ui.formatDateTimeLabel(clip.published_at) }) : t("clips.unpublished"),
            )}</small></span>
          </label>
          <p class="field-hint">${ui.esc(t("clips.manualPublishOnly"))}</p>
        </section>
        ${ready ? youtubePanelHtml(clip) : ""}
        <section class="form-section">
          <div class="form-section-header">
            <div><h3>${ui.esc(t("clips.sourceJob"))}</h3><p>${ui.esc(t("clips.sourceJobHint"))}</p></div>
          </div>
          <p><a class="text-link" href="#/video/${ui.attr(clip.source_job_id)}">${ui.esc(
            source ? ui.jobTitle(source) : clip.source_job_id.slice(0, 12),
          )}</a></p>
        </section>
      </div>
      <form class="stack" id="clip-metadata-form" data-clip-id="${ui.attr(clip.id)}">
        <section class="form-section">
          <div class="form-section-header">
            <div><h3>${ui.esc(t("clips.metadata"))}</h3><p>${ui.esc(t("clips.metadataHint"))}</p></div>
            <span class="tag" id="clip-metadata-status">${ui.esc(metadataStatus)}</span>
          </div>
          ${metadataError}
          <label>${ui.esc(t("clips.titleField"))}
            <input id="clip-title" maxlength="300" value="${ui.attr(clip.title || "")}" placeholder="${ui.attr(
              title,
            )}" />
          </label>
          <label>${ui.esc(t("clips.descriptionField"))}
            <textarea id="clip-description" rows="7" placeholder="${ui.attr(
              t("clips.descriptionPlaceholder"),
            )}">${ui.esc(clip.description || "")}</textarea>
          </label>
          <label>${ui.esc(t("clips.hashtagsField"))}
            <input id="clip-hashtags" value="${ui.attr(hashtagsText)}" placeholder="#shorts #clips" />
            <span class="field-hint">${ui.esc(t("clips.hashtagsHint"))}</span>
          </label>
          <div class="segmented">
            <button class="button button-primary" type="button" id="clip-metadata-save" ${
              ready ? "" : "disabled"
            }>${ui.icon("check")}<span>${ui.esc(t("common.save"))}</span></button>
            <button class="button button-secondary" type="button" id="clip-metadata-regenerate" ${
              ready ? "" : "disabled"
            }>${ui.icon("spark")}<span>${ui.esc(t("clips.regenerate"))}</span></button>
          </div>
        </section>
        <section class="form-section">
          <div class="form-section-header">
            <div><h3>${ui.esc(t("clips.copyActions"))}</h3><p>${ui.esc(t("clips.copyHint"))}</p></div>
          </div>
          <div class="copy-grid">
            <button class="button button-secondary button-small" type="button" data-copy-field="title">${ui.icon(
              "copy",
            )}<span>${ui.esc(t("clips.copyTitle"))}</span></button>
            <button class="button button-secondary button-small" type="button" data-copy-field="description">${ui.icon(
              "copy",
            )}<span>${ui.esc(t("clips.copyDescription"))}</span></button>
            <button class="button button-secondary button-small" type="button" data-copy-field="hashtags">${ui.icon(
              "copy",
            )}<span>${ui.esc(t("clips.copyHashtags"))}</span></button>
            <button class="button button-primary button-small" type="button" data-copy-field="all">${ui.icon(
              "copy",
            )}<span>${ui.esc(t("clips.copyAll"))}</span></button>
          </div>
          <div class="notice">${ui.icon("info")}<div>${ui.esc(t("clips.copyFormatHint"))}</div></div>
          <div id="clip-metadata-feedback" class="notice" hidden aria-live="polite"></div>
        </section>
      </form>
    </div>
  </div>`;

  return {
    title,
    html,
    mount() {
      if (state.token !== token) return;
      const form = main.querySelector("#clip-metadata-form");
      const feedback = main.querySelector("#clip-metadata-feedback");
      const showFeedback = (message, type = "info") => {
        if (!feedback) return;
        feedback.hidden = false;
        feedback.className = `notice ${type === "error" ? "is-error" : type === "warning" ? "is-warning" : ""}`;
        feedback.innerHTML = `${ui.icon(type === "error" || type === "warning" ? "alert" : "info")}<div>${ui.esc(
          message,
        )}</div>`;
      };
      const values = () => ({
        title: main.querySelector("#clip-title")?.value || "",
        description: main.querySelector("#clip-description")?.value || "",
        hashtags: main.querySelector("#clip-hashtags")?.value || "",
      });
      main.querySelector("#clip-metadata-save")?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        ui.setBusy(button, true, t("common.saving"));
        try {
          const payload = values();
          await api.updateClipMetadata(clip.id, {
            ...payload,
            hashtags: payload.hashtags.split(/[\s,،]+/).filter(Boolean),
          });
          ui.toast(t("clips.metadataSaved"), "success");
          renderRoute();
        } catch (error) {
          ui.setBusy(button, false);
          showFeedback(error.message || t("common.error"), "error");
        }
      });
      main.querySelector("#clip-metadata-regenerate")?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        if (clip.metadata_status === "manually_edited") {
          const confirmed = await ui.confirmDialog(t("clips.regenerateOverwriteConfirm"), {
            title: t("clips.regenerate"),
            confirmLabel: t("clips.regenerate"),
            danger: true,
          });
          if (!confirmed) return;
        }
        ui.setBusy(button, true, t("clips.generatingMetadata"));
        try {
          await api.regenerateClipMetadata(clip.id, { force: true });
          ui.toast(t("clips.metadataGenerated"), "success");
          renderRoute();
        } catch (error) {
          ui.setBusy(button, false);
          showFeedback(error.message || t("common.error"), "error");
        }
      });
      if (ready) mountYoutubePanel(main, clip);
      main.querySelector("#clip-published")?.addEventListener("change", async (event) => {
        const checkbox = event.currentTarget;
        try {
          await api.setClipPublished(clip.id, checkbox.checked);
          ui.toast(
            checkbox.checked ? t("clips.markedPublished") : t("clips.markedUnpublished"),
            "success",
          );
          renderRoute();
        } catch (error) {
          checkbox.checked = !checkbox.checked;
          showFeedback(error.message || t("common.error"), "error");
        }
      });
      form?.addEventListener("click", async (event) => {
        const button = event.target.closest("[data-copy-field]");
        if (!button) return;
        const current = values();
        const text =
          button.dataset.copyField === "all"
            ? [current.title, current.description, current.hashtags].filter(Boolean).join("\n\n")
            : current[button.dataset.copyField] || "";
        if (!text.trim()) {
          showFeedback(t("clips.copyEmpty"), "warning");
          return;
        }
        try {
          await ui.copyText(text);
          ui.toast(t("common.copied"), "success");
        } catch {
          showFeedback(t("clips.copyFailed"), "error");
        }
      });
    },
  };
}

async function trashView(token) {
  const data = await api.listTrash();
  let items = data.items || [];
  const selected = new Set();
  const keyFor = (item) => `${item.kind}:${item.id}`;

  const html = `<div class="page-enter">
    ${pageHead({
      context: t("nav.trash"),
      title: t("trash.title"),
      description: t("trash.description"),
      actions: `<a class="button button-secondary" href="#/library">${ui.icon("library")}<span>${ui.esc(
        t("nav.library"),
      )}</span></a>
      <a class="button button-secondary" href="#/clips">${ui.icon("clips")}<span>${ui.esc(
        t("nav.clips"),
      )}</span></a>`,
    })}
    <div class="toolbar toolbar-wrap">
      <div class="toolbar-actions">
        <button class="button button-ghost button-small" type="button" id="trash-select-all">${ui.icon(
          "check",
        )}<span>${ui.esc(t("common.selectAll"))}</span></button>
        <button class="button button-ghost button-small" type="button" id="trash-clear">${ui.icon(
          "cancel",
        )}<span>${ui.esc(t("common.clear"))}</span></button>
        <button class="button button-primary button-small" type="button" id="trash-restore-selected" disabled>${ui.icon(
          "retry",
        )}<span>${ui.esc(t("trash.restore"))}</span></button>
        <button class="button button-danger button-small" type="button" id="trash-purge-selected" disabled>${ui.icon(
          "trash",
        )}<span>${ui.esc(t("trash.deleteForever"))}</span></button>
        <button class="button button-danger button-small" type="button" id="trash-empty">${ui.icon(
          "trash",
        )}<span>${ui.esc(t("trash.empty"))}</span></button>
      </div>
      <div class="toolbar-spacer"></div>
      <span class="small muted" id="trash-count"></span>
    </div>
    <div class="notice">${ui.icon("info")}<div>${ui.esc(t("trash.retentionNote"))}</div></div>
    <div class="list-surface" style="margin-block-start:14px">
      <div class="list-head">
        <span>${ui.esc(t("trash.item"))}</span>
        <span>${ui.esc(t("common.type"))}</span>
        <span>${ui.esc(t("trash.deletedAt"))}</span>
        <span>${ui.esc(t("common.status"))}</span>
        <span style="text-align:end">${ui.esc(t("common.actions"))}</span>
      </div>
      <div class="list-body" id="trash-list" aria-live="polite"></div>
    </div>
  </div>`;

  function paintSelection() {
    const buttonRestore = main.querySelector("#trash-restore-selected");
    const buttonPurge = main.querySelector("#trash-purge-selected");
    if (buttonRestore) buttonRestore.disabled = selected.size === 0;
    if (buttonPurge) buttonPurge.disabled = selected.size === 0;
  }

  function row(item) {
    const key = keyFor(item);
    return `<div class="job-row trash-row" data-kind="${ui.attr(item.kind)}" data-id="${ui.attr(
      item.id,
    )}">
      <div class="job-row-main">
        <label class="row-select"><input type="checkbox" data-select-trash="${ui.attr(key)}" ${
          selected.has(key) ? "checked" : ""
        } aria-label="${ui.attr(t("common.select"))}"/></label>
        <div>
          <strong>${ui.esc(item.title || item.id)}</strong>
          <div class="job-sub">${ui.esc(item.id)}</div>
        </div>
      </div>
      <span class="tag ${item.kind === "clip" ? "is-accent" : ""}">${ui.esc(
        item.kind === "clip" ? t("common.clip") : t("common.video"),
      )}</span>
      <span>${ui.esc(ui.formatDateTimeLabel(item.deleted_at))}</span>
      <span>${ui.esc(ui.statusLabel(item.status))}</span>
      <div class="job-actions">
        <button class="button button-ghost button-small" type="button" data-action="restore-trash" data-kind="${ui.attr(
          item.kind,
        )}" data-id="${ui.attr(item.id)}">${ui.esc(t("trash.restore"))}</button>
        <button class="button button-ghost button-small" type="button" data-action="purge-trash" data-kind="${ui.attr(
          item.kind,
        )}" data-id="${ui.attr(item.id)}">${ui.esc(t("trash.deleteForever"))}</button>
      </div>
    </div>`;
  }

  function paint() {
    const holder = main.querySelector("#trash-list");
    const count = main.querySelector("#trash-count");
    if (!holder) return;
    if (count) count.textContent = ui.formatNumberLabel(items.length);
    if (!items.length) {
      holder.innerHTML = ui.renderEmpty({
        iconName: "trash",
        title: t("trash.emptyTitle"),
        body: t("trash.emptyBody"),
      });
    } else {
      holder.innerHTML = `<div class="row-list">${items.map(row).join("")}</div>`;
    }
    paintSelection();
  }

  async function runAction(itemsToApply, permanent) {
    if (!itemsToApply.length) return;
    const confirmed = await ui.confirmDialog(
      permanent
        ? t("trash.purgeConfirm", { count: itemsToApply.length })
        : t("trash.restoreConfirm", { count: itemsToApply.length }),
      {
        title: permanent ? t("trash.deleteForever") : t("trash.restore"),
        confirmLabel: permanent ? t("trash.deleteForever") : t("trash.restore"),
        danger: permanent,
      },
    );
    if (!confirmed) return;
    try {
      const payload = itemsToApply.map((item) => ({ kind: item.kind, id: item.id }));
      const result = permanent
        ? await api.purgeTrash(payload)
        : await api.restoreTrash(payload);
      if (result.errors?.length) {
        ui.toast(t("trash.partial", { count: result.errors.length }), "warning");
      } else {
        ui.toast(
          permanent
            ? t("trash.purged", { count: result.count || payload.length })
            : t("trash.restored", { count: result.count || payload.length }),
          "success",
        );
      }
      selected.clear();
      renderRoute();
    } catch (error) {
      ui.toast(error.message || t("common.error"), "error");
    }
  }

  return {
    title: t("trash.title"),
    html,
    mount() {
      paint();
      main.querySelector("#trash-select-all")?.addEventListener("click", () => {
        items.forEach((item) => selected.add(keyFor(item)));
        paint();
      });
      main.querySelector("#trash-clear")?.addEventListener("click", () => {
        selected.clear();
        paint();
      });
      main.querySelector("#trash-list")?.addEventListener("change", (event) => {
        const checkbox = event.target.closest("[data-select-trash]");
        if (!checkbox) return;
        if (checkbox.checked) selected.add(checkbox.dataset.selectTrash);
        else selected.delete(checkbox.dataset.selectTrash);
        paintSelection();
      });
      main.querySelector("#trash-restore-selected")?.addEventListener("click", () => {
        runAction(items.filter((item) => selected.has(keyFor(item))), false);
      });
      main.querySelector("#trash-purge-selected")?.addEventListener("click", () => {
        runAction(items.filter((item) => selected.has(keyFor(item))), true);
      });
      main.querySelector("#trash-empty")?.addEventListener("click", async () => {
        if (!items.length) return;
        const confirmed = await ui.confirmDialog(t("trash.emptyConfirm"), {
          title: t("trash.empty"),
          confirmLabel: t("trash.empty"),
          danger: true,
        });
        if (!confirmed) return;
        try {
          const result = await api.emptyTrash();
          ui.toast(t("trash.purged", { count: result.count || items.length }), "success");
          selected.clear();
          renderRoute();
        } catch (error) {
          ui.toast(error.message || t("common.error"), "error");
        }
      });
    },
  };
}

async function jobsView(token) {
  const [videoData, clipData] = await Promise.all([
    api.listVideoJobs({ limit: 200 }),
    api.listClipJobs({ limit: 200 }),
  ]);
  let videoJobs = videoData.jobs || [];
  let clipJobs = clipData.jobs || [];
  await loadSourceJobs(clipJobs);
  let typeFilter = "all";
  let statusFilter = "all";
  let lastRefresh = new Date();

  const html = `<div class="page-enter">
    ${pageHead({
      context: t("dashboard.context"),
      title: t("jobs.title"),
      description: t("jobs.description"),
      actions: `<a class="button button-secondary" href="#/library">${ui.icon("upload")}<span>${ui.esc(
        t("common.uploadVideo"),
      )}</span></a>`,
    })}
    <div class="toolbar">
      <div class="filter-row" id="job-type-filters" role="group" aria-label="${ui.attr(t("jobs.type"))}"></div>
      <div class="toolbar-spacer"></div>
      <div class="filter-row" id="job-status-filters" role="group" aria-label="${ui.attr(
        t("common.status"),
      )}"></div>
    </div>
    <div class="list-surface">
      <div class="table-wrap" style="border:0;border-radius:0">
        <table>
          <thead><tr>
            <th>${ui.esc(t("jobs.item"))}</th>
            <th>${ui.esc(t("jobs.type"))}</th>
            <th>${ui.esc(t("jobs.stage"))}</th>
            <th>${ui.esc(t("jobs.progress"))}</th>
            <th>${ui.esc(t("jobs.updated"))}</th>
            <th style="text-align:end">${ui.esc(t("jobs.actions"))}</th>
          </tr></thead>
          <tbody id="jobs-body"></tbody>
        </table>
      </div>
    </div>
    <p class="small muted" id="jobs-refresh" style="margin-block-start:10px">${ui.esc(
      t("jobs.autoRefresh"),
    )}</p>
  </div>`;

  function allItems() {
    return [
      ...videoJobs.map((job) => ({ kind: "video", job })),
      ...clipJobs.map((clip) => ({ kind: "clip", job: clip })),
    ].sort((a, b) => String(b.job.updated_at || "").localeCompare(String(a.job.updated_at || "")));
  }

  function filtered() {
    return allItems().filter((item) => {
      if (typeFilter !== "all" && item.kind !== typeFilter) return false;
      if (statusFilter !== "all" && item.job.status !== statusFilter) return false;
      return true;
    });
  }

  function paintFilters() {
    const typeHolder = main.querySelector("#job-type-filters");
    const statusHolder = main.querySelector("#job-status-filters");
    if (typeHolder) {
      const items = [
        ["all", t("jobs.allTypes"), videoJobs.length + clipJobs.length],
        ["video", t("jobs.videoJobs"), videoJobs.length],
        ["clip", t("jobs.clipJobs"), clipJobs.length],
      ];
      typeHolder.innerHTML = items
        .map(
          ([value, label, count]) => `<button class="filter-chip ${typeFilter === value ? "is-active" : ""}" type="button" data-type-filter="${value}" aria-pressed="${
            typeFilter === value
          }">${ui.esc(label)}<small>${ui.formatNumberLabel(count)}</small></button>`,
        )
        .join("");
    }
    if (statusHolder) {
      const items = ["all", "queued", "processing", "ready", "failed", "cancelled"];
      statusHolder.innerHTML = items
        .map(
          (value) => `<button class="filter-chip ${statusFilter === value ? "is-active" : ""}" type="button" data-status-filter="${value}" aria-pressed="${
            statusFilter === value
          }">${ui.esc(value === "all" ? t("jobs.allStatuses") : ui.statusLabel(value))}</button>`,
        )
        .join("");
    }
  }

  function rowFor(item) {
    const { kind, job } = item;
    const isVideo = kind === "video";
    const source = isVideo ? job : sourceJobFor(job);
    const title = isVideo ? ui.jobTitle(job) : clipTitle(job, source);
    const typeLabel = isVideo ? t("common.video") : t("common.clip");
    const openHref = isVideo ? `#/video/${job.id}` : "#/clips";
    const actions = isVideo
      ? `<a class="button button-ghost button-small" href="${openHref}">${ui.esc(t("jobs.open"))}</a>
        ${
          canRetryVideo(job)
            ? `<button class="button button-ghost button-small" type="button" data-action="retry-video" data-job-id="${ui.attr(
                job.id,
              )}">${ui.esc(t("common.retry"))}</button>`
            : ""
        }
        ${
          canCancelVideo(job)
            ? `<button class="button button-ghost button-small" type="button" data-action="cancel-video" data-job-id="${ui.attr(
                job.id,
              )}">${ui.esc(t("common.cancel"))}</button>`
            : ""
        }`
      : `<button class="button button-ghost button-small" type="button" data-action="preview-clip" data-clip-id="${ui.attr(
          job.id,
        )}">${ui.esc(t("clips.preview"))}</button>
        ${
          canRetryClip(job)
            ? `<button class="button button-ghost button-small" type="button" data-action="retry-clip" data-clip-id="${ui.attr(
                job.id,
              )}">${ui.esc(t("common.retry"))}</button>`
            : ""
        }
        ${
          canCancelClip(job)
            ? `<button class="button button-ghost button-small" type="button" data-action="cancel-clip" data-clip-id="${ui.attr(
                job.id,
              )}">${ui.esc(t("common.cancel"))}</button>`
            : ""
        }`;
    return `<tr>
      <td><a class="job-title" href="${openHref}" title="${ui.attr(title)}">${ui.esc(title)}</a>
        <div class="job-sub">${ui.esc(isVideo ? ui.jobSubtitle(job) : `${job.source_job_id.slice(0, 8)} · ${clipMeta(job)}`)}</div></td>
      <td><span class="tag ${isVideo ? "" : "is-accent"}">${ui.esc(typeLabel)}</span></td>
      <td>${ui.esc(ui.formatStage(job.stage))}</td>
      <td style="min-width:150px">${compactProgress(job.status, job.progress, job.stage)}</td>
      <td>${ui.esc(ui.formatRelativeLabel(job.updated_at))}</td>
      <td><div class="job-actions">${actions}</div></td>
    </tr>`;
  }

  function paint() {
    const body = main.querySelector("#jobs-body");
    if (!body) return;
    const rows = filtered();
    if (!allItems().length) {
      body.innerHTML = `<tr><td colspan="6">${ui.renderEmpty({
        iconName: "jobs",
        title: t("jobs.emptyTitle"),
        body: t("jobs.emptyBody"),
      })}</td></tr>`;
    } else if (!rows.length) {
      body.innerHTML = `<tr><td colspan="6">${ui.renderEmpty({
        iconName: "search",
        title: t("jobs.filteredEmpty"),
        body: t("jobs.allStatuses"),
      })}</td></tr>`;
    } else {
      body.innerHTML = rows.map(rowFor).join("");
    }
    paintFilters();
    const refresh = main.querySelector("#jobs-refresh");
    if (refresh) refresh.textContent = t("jobs.lastRefresh", { time: ui.formatRelativeLabel(lastRefresh) });
  }

  return {
    title: t("jobs.title"),
    html,
    mount() {
      paint();
      main.querySelector("#job-type-filters")?.addEventListener("click", (event) => {
        const chip = event.target.closest("[data-type-filter]");
        if (!chip) return;
        typeFilter = chip.dataset.typeFilter;
        paint();
      });
      main.querySelector("#job-status-filters")?.addEventListener("click", (event) => {
        const chip = event.target.closest("[data-status-filter]");
        if (!chip) return;
        statusFilter = chip.dataset.statusFilter;
        paint();
      });
      startPolling(
        async ({ signal }) => {
          const [nextVideo, nextClip] = await Promise.all([
            api.listVideoJobs({ limit: 200 }, { signal }),
            api.listClipJobs({ limit: 200 }, { signal }),
          ]);
          const nextVideoJobs = nextVideo.jobs || [];
          const nextClipJobs = nextClip.jobs || [];
          if (sameJson(nextVideoJobs, videoJobs) && sameJson(nextClipJobs, clipJobs)) return;
          videoJobs = nextVideoJobs;
          clipJobs = nextClipJobs;
          await loadSourceJobs(clipJobs, signal);
          lastRefresh = new Date();
          paint();
        },
        {
          interval: 4000,
          token,
          isActive: () => videoJobs.some(isJobActive) || clipJobs.some(isClipActive),
        },
      );
    },
  };
}

async function skillsView(token) {
  const data = await api.skills();
  const skills = data.skills || [];
  const tools = data.tools || [];
  const cards = skills
    .map((skill) => {
      const permissions = skill.permissions || {};
      const allowed = Object.entries(permissions)
        .filter(([, value]) => value)
        .map(([key]) => key);
      return `<article class="provider-card">
        <div class="provider-card-top">
          <span class="provider-mark ${skill.enabled ? "is-ready" : ""}">${ui.esc(
            skill.id.slice(0, 2).toUpperCase(),
          )}</span>
          <div>
            <h3>${ui.esc(skill.name)}</h3>
            <p>${ui.esc(skill.id)} · ${ui.esc(skill.category || "other")}</p>
          </div>
          <span class="provider-state ${skill.enabled ? "is-ready" : ""}" style="margin-inline-start:auto">${ui.esc(
            skill.enabled ? t("skills.enabled") : t("skills.disabled"),
          )}</span>
        </div>
        <p>${ui.esc(skill.description || "")}</p>
        <div class="signal-list">
          <span>${ui.esc(skill.source)}</span>
          <span>${ui.esc(skill.reviewed ? t("skills.reviewed") : t("skills.reviewRequired"))}</span>
          <span>${ui.esc(allowed.length ? allowed.join(", ") : t("skills.noPermissions"))}</span>
        </div>
        <p class="small muted">${ui.esc((skill.tools || []).join(", "))}</p>
        <div class="provider-card-actions">
          <button class="button button-secondary button-small" type="button" data-action="skill-toggle" data-skill="${ui.attr(
            skill.id,
          )}" data-enabled="${skill.enabled ? "false" : "true"}">${ui.esc(
            skill.enabled ? t("skills.disable") : t("skills.enable"),
          )}</button>
          ${
            !skill.reviewed
              ? `<button class="button button-primary button-small" type="button" data-action="skill-review" data-skill="${ui.attr(
                  skill.id,
                )}">${ui.esc(t("skills.review"))}</button>`
              : ""
          }
        </div>
      </article>`;
    })
    .join("");

  const toolRows = tools
    .map(
      (tool) => `<tr>
        <td><span class="mono">${ui.esc(tool.name)}</span></td>
        <td>${ui.esc(tool.description)}</td>
        <td>${ui.esc(
          [
            tool.requires_network ? "network" : "",
            tool.requires_filesystem ? "filesystem" : "",
            tool.requires_subprocess ? "subprocess" : "",
          ]
            .filter(Boolean)
            .join(", ") || t("skills.none"),
        )}</td>
      </tr>`,
    )
    .join("");

  return {
    title: t("skills.title"),
    html: `<div class="page-enter">
      ${pageHead({
        context: t("dashboard.context"),
        title: t("skills.title"),
        description: t("skills.description"),
      })}
      <div class="notice" style="margin-block-end:18px">${ui.icon("info")}<div>${ui.esc(
        t("skills.externalNote", { dir: data.external_dir || "config/skills" }),
      )}</div></div>
      <div class="stack">
        <section>
          <div class="section-title"><h2>${ui.esc(t("skills.installed"))}</h2><span class="tag">${ui.formatNumberLabel(
            skills.length,
          )}</span></div>
          ${
            cards
              ? `<div class="provider-grid">${cards}</div>`
              : ui.renderEmpty({
                  iconName: "spark",
                  title: t("skills.noSkills"),
                  body: t("skills.externalNote", { dir: data.external_dir || "config/skills" }),
                })
          }
        </section>
        <section class="panel">
          <div class="panel-header"><h2>${ui.esc(t("skills.tools"))}</h2></div>
          <div class="panel-body">
            ${
              toolRows
                ? `<div class="table-wrap"><table><thead><tr><th>${ui.esc(
                    t("skills.tool"),
                  )}</th><th>${ui.esc(t("skills.purpose"))}</th><th>${ui.esc(
                    t("skills.permissions"),
                  )}</th></tr></thead><tbody>${toolRows}</tbody></table></div>`
                : `<p class="muted small">${ui.esc(t("skills.noSkills"))}</p>`
            }
          </div>
        </section>
      </div>
    </div>`,
    mount() {
      if (state.token !== token) return;
    },
  };
}

async function providersView(token) {
  const [status, modelsData, usage] = await Promise.all([
    api.status(false),
    api.models(),
    api.usage(7, 50),
  ]);
  const providers = status.providers || [];
  const models = modelsData.models || [];
  const totals = usage.totals || {};
  const summary = usage.summary || [];
  const recent = usage.recent || [];
  const paths = status.paths || {};

  const providerCards = providers
    .map((provider) => {
      const providerState = providerStatus(provider);
      return `<article class="provider-card">
        <div class="provider-card-top">
          <span class="provider-mark ${providerState.ready ? "is-ready" : ""}">${ui.esc(
            provider.id.slice(0, 2).toUpperCase(),
          )}</span>
          <div>
            <h3>${ui.esc(provider.id)}</h3>
            <p>${ui.esc(provider.type)} · ${ui.formatNumberLabel(provider.models)} ${ui.esc(
              t("common.models"),
            )}</p>
          </div>
          <span class="provider-state ${providerState.ready ? "is-ready" : ""}" style="margin-inline-start:auto">${ui.esc(
            providerState.label,
          )}</span>
        </div>
        <div class="signal-list">
          ${(provider.capabilities || []).map((capability) => `<span>${ui.esc(capability)}</span>`).join("")}
          <span>${ui.esc(provider.enabled ? t("common.enabled") : t("common.disabled"))}</span>
          <span>${ui.esc(provider.key_present ? t("common.keyPresent") : t("common.keyMissing"))}</span>
        </div>
        <p>${ui.esc(provider.reason || (providerState.ready ? t("providers.ready") : t("providers.notReadyBody")))}</p>
        ${
          provider.catalog
            ? `<p class="small muted">${
                provider.catalog.updated_at
                  ? ui.esc(
                      t("providers.catalogUpdated", {
                        time: ui.formatDateTimeLabel(provider.catalog.updated_at),
                      }),
                    )
                  : ui.esc(t("providers.catalogEmpty"))
              } · ${ui.esc(
                `${ui.formatNumberLabel(provider.catalog.models || 0)} ${t("common.models")}`,
              )}</p>`
            : ""
        }
        <div class="provider-card-actions">
          <button class="button button-secondary button-small" type="button" data-action="test-provider" data-provider="${ui.attr(
            provider.id,
          )}">${ui.icon("spark")}<span>${ui.esc(t("common.testConnection"))}</span></button>
          ${
            provider.id === "nvidia"
              ? `<button class="button button-secondary button-small" type="button" data-action="discover-nvidia">${ui.icon(
                  "refresh",
                )}<span>${ui.esc(t("providers.discoverNvidia"))}</span></button>`
              : ""
          }
          <button class="button button-secondary button-small" type="button" data-action="provider-toggle" data-provider="${ui.attr(
            provider.id,
          )}" data-enabled="${provider.enabled ? "false" : "true"}">${ui.esc(
            provider.enabled ? t("providers.disable") : t("providers.enable"),
          )}</button>
          ${
            provider.last_health
              ? `<span class="tag ${provider.last_health.ok ? "is-teal" : ""}">${ui.esc(
                  provider.last_health.ok ? t("common.online") : t("common.failed"),
                )}</span>`
              : ""
          }
        </div>
      </article>`;
    })
    .join("");

  const modelRows = models
    .map((model) => {
      const eligibility = Object.entries(model.eligibility || {});
      const allowed = eligibility.filter(([, verdict]) => verdict.allowed).length;
      return `<tr>
        <td><span class="mono">${ui.esc(model.id)}</span><div class="job-sub">${ui.esc(
          model.name || "",
        )}</div></td>
        <td>${ui.esc(model.provider)}</td>
        <td>${ui.esc(t(`providers.access.${model.access_type || "unknown"}`))}</td>
        <td>${(model.tasks || []).map((task) => `<span class="tag">${ui.esc(task)}</span>`).join(" ")}</td>
        <td>${model.free ? `<span class="tag is-teal">${ui.esc(t("providers.freeApproved"))}</span>` : ""}</td>
        <td>${ui.esc(`${allowed}/${eligibility.length}`)}</td>
        <td>${ui.esc(model.verification_status || "unverified")}</td>
        <td>${model.ready ? `<span class="tag is-teal">${ui.esc(t("providers.modelReady"))}</span>` : `<span class="tag">${ui.esc(t("providers.modelNotReady"))}</span>`}</td>
        <td>${ui.esc(model.enabled ? t("common.enabled") : t("common.disabled"))}</td>
      </tr>`;
    })
    .join("");

  const usageRows = recent
    .map(
      (event) => `<tr>
        <td>${ui.esc(ui.formatDateTimeLabel(event.ts))}</td>
        <td>${ui.esc(event.provider)}</td>
        <td>${ui.esc(event.model || "—")}</td>
        <td>${ui.esc(event.task || "—")}</td>
        <td>${event.success ? `<span class="tag is-teal">${ui.esc(t("common.success"))}</span>` : `<span class="tag">${ui.esc(t("common.failed"))}</span>`}</td>
        <td dir="ltr">${ui.formatNumberLabel(event.audio_seconds || 0)}s</td>
        <td dir="ltr">${ui.formatNumberLabel((event.input_tokens || 0) + (event.output_tokens || 0))}</td>
      </tr>`,
    )
    .join("");

  const html = `<div class="page-enter">
    ${pageHead({
      context: t("dashboard.context"),
      title: t("providers.title"),
      description: t("providers.description"),
      actions: `<button class="button button-secondary" type="button" data-action="reload-providers">${ui.icon(
        "refresh",
      )}<span>${ui.esc(t("providers.reload"))}</span></button>`,
    })}
    <div class="notice ${status.ready ? "" : "is-warning"}" style="margin-block-end:18px">
      ${ui.icon(status.ready ? "check" : "alert")}
      <div><strong>${ui.esc(status.ready ? t("providers.ready") : t("providers.notReady"))}</strong> ${ui.esc(
        status.ready ? t("providers.localPolicy") : t("providers.notReadyBody"),
      )}</div>
    </div>
    <div class="stack">
      <section>
        <div class="section-title"><h2>${ui.esc(t("providers.providerList"))}</h2><span class="tag">${ui.esc(
          status.local_only ? t("nav.localOnly") : "",
        )}</span></div>
        ${
          providerCards
            ? `<div class="provider-grid">${providerCards}</div>`
            : ui.renderEmpty({
                iconName: "providers",
                title: t("providers.notReady"),
                body: t("providers.notReadyBody"),
              })
        }
      </section>
      <section class="panel">
        <div class="panel-header"><h2>${ui.esc(t("providers.modelList"))}</h2><span class="small muted">${ui.esc(
          `${ui.formatNumberLabel(models.length)} ${t("common.models")}`,
        )}</span></div>
        <div class="panel-body">
          ${
            modelRows
              ? `<div class="table-wrap"><table><thead><tr>
                  <th>${ui.esc(t("common.model"))}</th><th>${ui.esc(t("common.provider"))}</th>
                  <th>${ui.esc(t("providers.access"))}</th><th>${ui.esc(t("common.tasks"))}</th>
                  <th>${ui.esc(t("providers.freeApproved"))}</th><th>${ui.esc(t("providers.eligibility"))}</th>
                  <th>${ui.esc(t("providers.verification"))}</th><th>${ui.esc(t("providers.modelReady"))}</th>
                  <th>${ui.esc(t("common.status"))}</th>
                </tr></thead><tbody>${modelRows}</tbody></table></div>`
              : ui.renderEmpty({
                  iconName: "providers",
                  title: t("providers.noModels"),
                  body: t("providers.notReadyBody"),
                })
          }
        </div>
      </section>
      <section class="panel">
        <div class="panel-header"><h2>${ui.esc(t("providers.usage"))}</h2><span class="small muted">${ui.esc(
          t("providers.localUsageNote"),
        )}</span></div>
        <div class="panel-body">
          <div class="usage-grid">
            <div class="usage-stat"><span>${ui.esc(t("common.requests"))}</span><strong>${ui.formatNumberLabel(
              totals.requests || 0,
            )}</strong></div>
            <div class="usage-stat"><span>${ui.esc(t("common.successes"))}</span><strong>${ui.formatNumberLabel(
              totals.successes || 0,
            )}</strong></div>
            <div class="usage-stat"><span>${ui.esc(t("common.failures"))}</span><strong>${ui.formatNumberLabel(
              totals.failures || 0,
            )}</strong></div>
            <div class="usage-stat"><span>${ui.esc(t("common.audio"))}</span><strong dir="ltr">${ui.formatNumberLabel(
              Math.round(totals.audio_seconds || 0),
            )}s</strong></div>
          </div>
          <div class="section-title" style="margin-block-start:18px"><h3>${ui.esc(
            t("providers.recentUsage"),
          )}</h3><span class="small muted">${ui.esc(
            `${ui.formatNumberLabel(summary.length)} ${t("common.groups")}`,
          )}</span></div>
          ${
            usageRows
              ? `<div class="table-wrap"><table><thead><tr>
                  <th>${ui.esc(t("common.time"))}</th><th>${ui.esc(t("common.provider"))}</th>
                  <th>${ui.esc(t("common.model"))}</th><th>${ui.esc(t("common.type"))}</th>
                  <th>${ui.esc(t("common.status"))}</th><th>${ui.esc(t("common.audio"))}</th>
                  <th>${ui.esc(t("common.tokens"))}</th>
                </tr></thead><tbody>${usageRows}</tbody></table></div>`
              : `<p class="muted small">${ui.esc(t("providers.noUsage"))}</p>`
          }
        </div>
      </section>
      <section class="panel">
        <div class="panel-header"><h2>${ui.esc(t("providers.paths"))}</h2></div>
        <div class="panel-body">
          <div class="info-grid">
            <div class="info-cell"><span>${ui.esc(t("providers.configDir"))}</span><strong class="mono" dir="ltr">${ui.esc(
              paths.config_dir || "—",
            )}</strong></div>
            <div class="info-cell"><span>${ui.esc(t("providers.dataDir"))}</span><strong class="mono" dir="ltr">${ui.esc(
              paths.data_dir || "—",
            )}</strong></div>
          </div>
          <p class="small muted" style="margin-block-start:12px">${ui.esc(
            (status.notes || []).join(" "),
          )}</p>
        </div>
      </section>
    </div>
  </div>`;

  return {
    title: t("providers.title"),
    html,
    mount() {
      if (state.token !== token) return;
    },
  };
}

async function handleGlobalAction(event) {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  if (target.tagName === "A") event.preventDefault();

  if (action === "retry-route") {
    renderRoute();
    return;
  }
  if (action === "upload-video") {
    await openUploadDialog();
    return;
  }
  if (action === "import-url") {
    openUrlDialog();
    return;
  }
  if (action === "retry-video") {
    await retryVideo(target.dataset.jobId, target);
    return;
  }
  if (action === "cancel-video") {
    await cancelVideo(target.dataset.jobId, target);
    return;
  }
  if (action === "retry-clip") {
    await retryClip(target.dataset.clipId, target);
    return;
  }
  if (action === "cancel-clip") {
    await cancelClip(target.dataset.clipId, target);
    return;
  }
  if (action === "render-clip") {
    await renderClipNow(target.dataset.clipId, target);
    return;
  }
  if (action === "preview-clip") {
    await openClipPreview(target.dataset.clipId);
    return;
  }
  if (action === "delete-video") {
    await softDeleteSource(target.dataset.jobId, target);
    return;
  }
  if (action === "delete-clip") {
    await softDeleteClip(target.dataset.clipId, target);
    return;
  }
  if (action === "restore-trash") {
    await restoreTrashItem(target.dataset.kind, target.dataset.id, target);
    return;
  }
  if (action === "purge-trash") {
    await purgeTrashItem(target.dataset.kind, target.dataset.id, target);
    return;
  }
  if (action === "reload-providers") {
    ui.setBusy(target, true, t("common.loading"));
    try {
      await api.reloadProviders();
      ui.toast(t("providers.reloaded"), "success");
      renderRoute();
      refreshSystem();
    } catch (error) {
      ui.setBusy(target, false);
      ui.toast(error.message || t("common.error"), "error");
    }
    return;
  }
  if (action === "test-provider") {
    await testProvider(target.dataset.provider, target);
    return;
  }
  if (action === "discover-nvidia") {
    ui.setBusy(target, true, t("providers.discoveringNvidia"));
    try {
      const result = await api.discoverProviderModels("nvidia", true);
      if (result?.ok) {
        ui.toast(
          t("providers.discoveredNvidia", {
            models: ui.formatNumberLabel(result.models || 0),
          }),
          "success",
        );
      } else {
        ui.toast(
          t("providers.discoverFailed", {
            message: result?.error || t("common.error"),
          }),
          "error",
        );
      }
      renderRoute();
      refreshSystem();
    } catch (error) {
      ui.setBusy(target, false);
      ui.toast(
        t("providers.discoverFailed", {
          message: error.message || t("common.error"),
        }),
        "error",
      );
    }
    return;
  }
  if (action === "provider-toggle") {
    const enabled = target.dataset.enabled === "true";
    ui.setBusy(target, true, t("common.loading"));
    try {
      await api.toggleProvider(target.dataset.provider, enabled);
      ui.toast(t("providers.toggled", { provider: target.dataset.provider }), "success");
      renderRoute();
      refreshSystem();
    } catch (error) {
      ui.setBusy(target, false);
      ui.toast(error.message || t("common.error"), "error");
    }
    return;
  }
  if (action === "skill-toggle") {
    const enabled = target.dataset.enabled === "true";
    ui.setBusy(target, true, t("common.loading"));
    try {
      await api.toggleSkill(target.dataset.skill, enabled);
      ui.toast(t("skills.updated"), "success");
      renderRoute();
    } catch (error) {
      ui.setBusy(target, false);
      ui.toast(error.message || t("common.error"), "error");
    }
    return;
  }
  if (action === "skill-review") {
    ui.setBusy(target, true, t("common.loading"));
    try {
      await api.reviewSkill(target.dataset.skill, true);
      ui.toast(t("skills.reviewed"), "success");
      renderRoute();
    } catch (error) {
      ui.setBusy(target, false);
      ui.toast(error.message || t("common.error"), "error");
    }
    return;
  }
  if (action === "create-clips") {
    await createClipsFromForm(target);
  }
}

async function retryVideo(jobId, button) {
  if (!jobId) return;
  const confirmed = await ui.confirmDialog(t("library.retryConfirm"), {
    title: t("library.retryJob"),
    confirmLabel: t("common.retry"),
  });
  if (!confirmed) return;
  ui.setBusy(button, true, t("common.loading"));
  try {
    await api.retryVideo(jobId);
    ui.toast(t("library.retryJob"), "success");
    renderRoute();
  } catch (error) {
    ui.setBusy(button, false);
    ui.toast(error.message || t("common.error"), "error");
  }
}

async function cancelVideo(jobId, button) {
  if (!jobId) return;
  const confirmed = await ui.confirmDialog(t("library.cancelConfirm"), {
    title: t("library.cancelJob"),
    confirmLabel: t("common.cancel"),
    danger: true,
  });
  if (!confirmed) return;
  ui.setBusy(button, true, t("common.loading"));
  try {
    await api.cancelVideo(jobId);
    ui.toast(t("library.cancelJob"), "success");
    renderRoute();
  } catch (error) {
    ui.setBusy(button, false);
    ui.toast(error.message || t("common.error"), "error");
  }
}

async function retryClip(clipId, button) {
  if (!clipId) return;
  const confirmed = await ui.confirmDialog(t("clips.retryConfirm"), {
    title: t("clips.retry"),
    confirmLabel: t("common.retry"),
  });
  if (!confirmed) return;
  ui.setBusy(button, true, t("common.loading"));
  try {
    await api.retryClip(clipId);
    ui.toast(t("clips.retryStarted"), "success");
    renderRoute();
  } catch (error) {
    ui.setBusy(button, false);
    ui.toast(error.message || t("common.error"), "error");
  }
}

async function cancelClip(clipId, button) {
  if (!clipId) return;
  const confirmed = await ui.confirmDialog(t("clips.cancelledConfirm"), {
    title: t("clips.cancel"),
    confirmLabel: t("common.cancel"),
    danger: true,
  });
  if (!confirmed) return;
  ui.setBusy(button, true, t("common.loading"));
  try {
    await api.cancelClip(clipId);
    ui.toast(t("clips.cancelled"), "success");
    renderRoute();
  } catch (error) {
    ui.setBusy(button, false);
    ui.toast(error.message || t("common.error"), "error");
  }
}

async function renderClipNow(clipId, button) {
  if (!clipId) return;
  ui.setBusy(button, true, t("common.loading"));
  try {
    await api.renderClip(clipId);
    ui.toast(t("clips.renderStarted"), "success");
    renderRoute();
  } catch (error) {
    ui.setBusy(button, false);
    ui.toast(error.message || t("common.error"), "error");
  }
}

async function softDeleteSource(jobId, button) {
  if (!jobId) return;
  const confirmed = await ui.confirmDialog(t("library.deleteConfirm"), {
    title: t("library.deleteSource"),
    confirmLabel: t("common.delete"),
    danger: true,
  });
  if (!confirmed) return;
  ui.setBusy(button, true, t("common.loading"));
  try {
    await api.deleteVideo(jobId);
    ui.toast(t("library.movedToTrash", { count: 1 }), "success");
    renderRoute();
  } catch (error) {
    ui.setBusy(button, false);
    ui.toast(error.message || t("common.error"), "error");
  }
}

async function softDeleteClip(clipId, button) {
  if (!clipId) return;
  const confirmed = await ui.confirmDialog(t("clips.deleteConfirm"), {
    title: t("clips.deleteClip"),
    confirmLabel: t("common.delete"),
    danger: true,
  });
  if (!confirmed) return;
  ui.setBusy(button, true, t("common.loading"));
  try {
    await api.deleteClip(clipId);
    ui.toast(t("clips.movedToTrash", { count: 1 }), "success");
    if (parseRoute().name === "clip") window.location.hash = "#/clips";
    else renderRoute();
  } catch (error) {
    ui.setBusy(button, false);
    ui.toast(error.message || t("common.error"), "error");
  }
}

async function restoreTrashItem(kind, id, button) {
  if (!kind || !id) return;
  ui.setBusy(button, true, t("common.loading"));
  try {
    const result = await api.restoreTrash([{ kind, id }]);
    if (result.errors?.length) {
      ui.toast(result.errors[0].message || t("common.error"), "error");
      ui.setBusy(button, false);
      return;
    }
    ui.toast(t("trash.restored", { count: 1 }), "success");
    renderRoute();
  } catch (error) {
    ui.setBusy(button, false);
    ui.toast(error.message || t("common.error"), "error");
  }
}

async function purgeTrashItem(kind, id, button) {
  if (!kind || !id) return;
  const confirmed = await ui.confirmDialog(t("trash.purgeOneConfirm"), {
    title: t("trash.deleteForever"),
    confirmLabel: t("trash.deleteForever"),
    danger: true,
  });
  if (!confirmed) return;
  ui.setBusy(button, true, t("common.loading"));
  try {
    const result = await api.purgeTrash([{ kind, id }]);
    if (result.errors?.length) {
      ui.toast(result.errors[0].message || t("common.error"), "error");
      ui.setBusy(button, false);
      return;
    }
    ui.toast(t("trash.purged", { count: 1 }), "success");
    renderRoute();
  } catch (error) {
    ui.setBusy(button, false);
    ui.toast(error.message || t("common.error"), "error");
  }
}

async function openClipPreview(clipId) {
  try {
    const clip = await api.getClipJob(clipId);
    let source = state.videoCache.get(clip.source_job_id) || null;
    if (!source) {
      try {
        source = await api.getVideoJob(clip.source_job_id);
        state.videoCache.set(clip.source_job_id, source);
      } catch {
        source = null;
      }
    }
    const title = clipTitle(clip, source);
    const ready = clip.status === "ready";
    const downloadUrl = api.clipDownloadUrl(clip.id);
    const media = ready
      ? `<video src="${downloadUrl}" controls playsinline style="width:100%;border-radius:10px;background:#000;max-height:56vh"></video>`
      : `<div class="notice">${ui.icon("info")}<div>${ui.esc(t("clips.noPreview"))}</div></div>`;
    const footer = `<button class="button button-secondary" type="button" data-close-dialog>${ui.esc(
      t("common.close"),
    )}</button>
      ${
        ready
          ? `<a class="button button-primary" href="${downloadUrl}" download>${ui.icon(
              "download",
            )}<span>${ui.esc(t("common.download"))}</span></a>`
          : ""
      }
      <a class="button button-ghost" href="#/video/${ui.attr(
        clip.source_job_id,
      )}?tab=highlights" data-close-dialog>${ui.esc(t("clips.openSource"))}</a>`;
    ui.openDialog({
      title,
      subtitle: `${formatName(clip.format)} · ${clip.width}×${clip.height} · ${ui.formatDuration(
        clip.duration,
      )} · ${qualityName(clip.quality)}`,
      body: `<div class="stack-sm">
        ${media}
        <div class="info-grid">
          <div class="info-cell"><span>${ui.esc(t("common.status"))}</span><strong>${ui.esc(
            ui.statusLabel(clip.status),
          )}</strong><small>${ui.esc(ui.formatStage(clip.stage))}</small></div>
          <div class="info-cell"><span>${ui.esc(t("common.size"))}</span><strong dir="ltr">${ui.formatBytes(
            clip.size_bytes,
          )}</strong></div>
          <div class="info-cell"><span>${ui.esc(t("common.captions"))}</span><strong>${ui.esc(
            clip.captions ? t("export.captionsOn") : t("export.captionsOff"),
          )}</strong><small>${ui.esc(clip.caption_language || "")}</small></div>
          <div class="info-cell"><span>${ui.esc(t("clips.sourceJob"))}</span><strong class="mono" dir="ltr">${ui.esc(
            clip.source_job_id.slice(0, 12),
          )}</strong><small>${ui.esc(source ? ui.jobTitle(source) : "")}</small></div>
        </div>
        ${
          clip.error?.message
            ? `<div class="notice is-error">${ui.icon("alert")}<div>${ui.esc(clip.error.message)}</div></div>`
            : ""
        }
      </div>`,
      footer,
      className: "preview-dialog",
    });
  } catch (error) {
    ui.toast(error.message || t("common.error"), "error");
  }
}

async function testProvider(providerId, button) {
  if (!providerId) return;
  ui.setBusy(button, true, t("common.testing"));
  ui.toast(t("providers.testingConnection", { provider: providerId }), "info", 2200);
  try {
    const result = await api.testConnection(providerId);
    if (result?.ok) {
      ui.toast(t("providers.connectionOk", { provider: providerId }), "success");
    } else {
      ui.toast(
        t("providers.connectionFailed", {
          provider: providerId,
          message: result?.message || t("common.error"),
        }),
        "error",
      );
    }
    ui.setBusy(button, false);
    refreshSystem();
  } catch (error) {
    ui.setBusy(button, false);
    ui.toast(
      t("providers.connectionFailed", {
        provider: providerId,
        message: error.message || t("common.error"),
      }),
      "error",
    );
  }
}

async function openUploadDialog() {
  let config = null;
  try {
    config = await api.videoConfig();
  } catch {
    config = null;
  }
  const extensions = config?.allowed_extensions || ALLOWED_EXTENSIONS;
  let selectedFile = null;
  let uploadButton = null;
  const dialog = ui.openDialog({
    title: t("library.uploadTitle"),
    subtitle: t("library.uploadBody"),
    body: `<form id="upload-form">
      <label class="drop-zone" id="drop-zone">
        <input id="file-input" type="file" accept="${ui.attr(extensions.join(","))}" />
        ${ui.icon("upload")}
        <strong>${ui.esc(t("library.uploadDrop"))}</strong>
        <span>${ui.esc(t("library.uploadHint"))}</span>
      </label>
      <div class="file-summary" id="file-summary" hidden></div>
      <div class="upload-progress" id="upload-progress" hidden>
        ${ui.progressBar(0, "processing", "uploading")}
      </div>
      <div id="upload-error" class="notice is-error" hidden></div>
    </form>`,
    footer: `<button class="button button-secondary" type="button" data-close-dialog>${ui.esc(
      t("common.cancel"),
    )}</button>
      <button class="button button-primary" type="button" id="upload-submit" disabled>${ui.icon(
        "upload",
      )}<span>${ui.esc(t("library.uploadStart"))}</span></button>`,
    onMount(dialogEl) {
      const input = dialogEl.querySelector("#file-input");
      const zone = dialogEl.querySelector("#drop-zone");
      const summary = dialogEl.querySelector("#file-summary");
      const progress = dialogEl.querySelector("#upload-progress");
      const errorBox = dialogEl.querySelector("#upload-error");
      uploadButton = dialogEl.querySelector("#upload-submit");

      const chooseFile = (file) => {
        if (!file) return;
        const extension = `.${file.name.split(".").pop()?.toLowerCase()}`;
        if (!extensions.includes(extension)) {
          errorBox.hidden = false;
          errorBox.innerHTML = `${ui.icon("alert")}<div>${ui.esc(
            `${file.name} · ${t("library.uploadHint")}`,
          )}</div>`;
          return;
        }
        selectedFile = file;
        errorBox.hidden = true;
        summary.hidden = false;
        summary.innerHTML = `<strong>${ui.esc(file.name)}</strong><span>${ui.esc(
          ui.formatBytes(file.size),
        )}</span>`;
        uploadButton.disabled = false;
      };

      input?.addEventListener("change", () => chooseFile(input.files?.[0]));
      zone?.addEventListener("dragover", (event) => {
        event.preventDefault();
        zone.classList.add("is-dragging");
      });
      zone?.addEventListener("dragleave", () => zone.classList.remove("is-dragging"));
      zone?.addEventListener("drop", (event) => {
        event.preventDefault();
        zone.classList.remove("is-dragging");
        chooseFile(event.dataTransfer?.files?.[0]);
      });
      uploadButton?.addEventListener("click", async () => {
        if (!selectedFile) return;
        ui.setBusy(uploadButton, true, t("library.uploading"));
        progress.hidden = false;
        try {
          const payload = await api.uploadVideo(selectedFile, (ratio) => {
            const track = progress.querySelector(".progress-fill");
            const label = progress.querySelector(".progress-label span:last-child");
            if (track) track.style.width = `${Math.round(ratio * 100)}%`;
            if (label) label.textContent = `${Math.round(ratio * 100)}%`;
          });
          const job = payload.job;
          ui.closeDialog();
          if (payload.duplicate) ui.toast(t("library.uploadDuplicate"), "warning");
          else ui.toast(t("common.success"), "success");
          go(`#/video/${job.id}`);
        } catch (error) {
          ui.setBusy(uploadButton, false);
          errorBox.hidden = false;
          errorBox.innerHTML = `${ui.icon("alert")}<div>${ui.esc(
            error.message || t("common.error"),
          )}</div>`;
        }
      });
    },
  });
  return dialog;
}

function openUrlDialog() {
  ui.openDialog({
    title: t("library.urlTitle"),
    subtitle: t("library.urlBody"),
    body: `<form id="url-form">
      <label>${ui.esc(t("library.urlLabel"))}
        <input id="url-input" type="url" inputmode="url" autocomplete="off" placeholder="${ui.attr(
          t("library.urlPlaceholder"),
        )}" />
      </label>
      <div id="url-error" class="notice is-error" hidden></div>
    </form>`,
    footer: `<button class="button button-secondary" type="button" data-close-dialog>${ui.esc(
      t("common.cancel"),
    )}</button>
      <button class="button button-primary" type="button" id="url-submit">${ui.icon("link")}<span>${ui.esc(
        t("library.urlStart"),
      )}</span></button>`,
    onMount(dialogEl) {
      const input = dialogEl.querySelector("#url-input");
      const button = dialogEl.querySelector("#url-submit");
      const errorBox = dialogEl.querySelector("#url-error");
      const submit = async () => {
        const value = input.value.trim();
        let url;
        try {
          url = new URL(value);
        } catch {
          errorBox.hidden = false;
          errorBox.innerHTML = `${ui.icon("alert")}<div>${ui.esc(t("library.urlPlaceholder"))}</div>`;
          return;
        }
        if (!["http:", "https:"].includes(url.protocol)) {
          errorBox.hidden = false;
          errorBox.innerHTML = `${ui.icon("alert")}<div>${ui.esc(t("library.urlPlaceholder"))}</div>`;
          return;
        }
        ui.setBusy(button, true, t("library.importing"));
        try {
          const payload = await api.addVideoUrl(value);
          const job = payload.job;
          ui.closeDialog();
          if (payload.duplicate) ui.toast(t("library.uploadDuplicate"), "warning");
          else ui.toast(t("common.success"), "success");
          go(`#/video/${job.id}`);
        } catch (error) {
          ui.setBusy(button, false);
          errorBox.hidden = false;
          errorBox.innerHTML = `${ui.icon("alert")}<div>${ui.esc(
            error.message || t("common.error"),
          )}</div>`;
        }
      };
      button?.addEventListener("click", submit);
      input?.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          submit();
        }
      });
      input?.focus();
    },
  });
}

async function createClipsFromForm(button) {
  const form = document.getElementById("export-form");
  if (!form) return;
  const jobId = button.dataset.jobId || form.dataset.jobId;
  const selected = [...form.querySelectorAll('input[name="highlight"]:checked')].map((input) =>
    Number(input.value),
  );
  const formats = [...form.querySelectorAll('input[name="format"]:checked')].map(
    (input) => input.value,
  );
  const quality = form.querySelector('input[name="quality"]:checked')?.value || "standard";
  const fit = form.querySelector('input[name="fit"]:checked')?.value || "crop";
  const captions = Boolean(form.querySelector("#captions-toggle")?.checked);
  const captionLanguage = form.querySelector("#caption-language")?.value || null;
  const render = form.querySelector('input[name="render-mode"]:checked')?.value === "now";
  const visualSettings = {
    visual_enhancement: Boolean(form.querySelector("#visual-enhancement")?.checked),
    intensity: form.querySelector("#visual-intensity")?.value || "balanced",
    captions,
    zoom: Boolean(form.querySelector("#visual-zoom")?.checked),
    punch: Boolean(form.querySelector("#visual-punch")?.checked),
    emphasis: Boolean(form.querySelector("#visual-emphasis")?.checked),
    transitions: Boolean(form.querySelector("#visual-transitions")?.checked),
    callouts: Boolean(form.querySelector("#visual-callouts")?.checked),
  };
  if (!selected.length) {
    ui.toast(t("export.noSelection"), "warning");
    return;
  }
  if (!formats.length) {
    ui.toast(t("export.noFormat"), "warning");
    return;
  }
  if (captions && !captionLanguage) {
    const sourceJob = state.videoCache.get(jobId);
    if (sourceJob && !sourceJob.transcript) {
      ui.toast(t("export.captionsNoTranscript"), "warning");
      return;
    }
  }
  const editor = state.trimEditor;
  let ranges = null;
  if (editor) {
    const validation = editor.validate(selected);
    if (!validation.ok) {
      editor.revealError(validation.index, validation.code);
      ui.toast(validation.message || t("trim.invalid"), "warning");
      return;
    }
    if (editor.hasCustomRanges(selected)) ranges = editor.rangesFor(selected);
  }
  ui.setBusy(button, true, render ? t("common.renderNow") : t("export.creating"));
  try {
    const payload = await api.createClips(jobId, {
      ...(ranges ? { ranges } : { highlight_indices: selected }),
      formats,
      quality,
      fit,
      captions,
      caption_language: captions ? captionLanguage || null : null,
      visual_settings: visualSettings,
      render,
    });
    const count = payload.jobs?.length || selected.length * formats.length;
    ui.toast(
      render
        ? t("export.createdRendering", { count })
        : t("export.created", { count }),
      "success",
    );
    go(`#/video/${jobId}?tab=clips`);
  } catch (error) {
    ui.setBusy(button, false);
    ui.toast(error.message || t("common.error"), "error");
  }
}
