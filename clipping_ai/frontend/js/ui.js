import { formatDateTime, formatNumber, formatRelative, getLocale, t } from "./i18n.js";

export function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function attr(value) {
  return esc(value);
}

export function icon(name, className = "") {
  return `<svg class="icon ${className}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
}

export function formatClock(seconds, precise = false) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "—";
  const value = Math.max(0, Number(seconds));
  const total = precise ? Math.round(value * 10) / 10 : Math.floor(value);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const secText = precise
    ? secs.toFixed(1).padStart(4, "0")
    : String(Math.floor(secs)).padStart(2, "0");
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${secText}`;
  }
  return `${String(minutes).padStart(2, "0")}:${secText}`;
}

export function formatDuration(seconds) {
  return formatClock(seconds);
}

export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || Number.isNaN(Number(bytes))) return "—";
  const value = Number(bytes);
  if (value < 1024) return `${formatNumber(value)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${new Intl.NumberFormat(getLocale() === "ar" ? "ar" : "en", {
    maximumFractionDigits: size >= 10 ? 0 : 1,
  }).format(size)} ${units[index]}`;
}

export function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return `${Math.round(Number(value) * 100)}%`;
}

export function formatConfidence(value) {
  if (value === null || value === undefined) return "—";
  return `${Math.round(Number(value) * 100)}%`;
}

export function formatStage(stage) {
  if (!stage) return "—";
  const key = `common.${stage}`;
  const label = t(key);
  return label === key ? String(stage).replaceAll("_", " ") : label;
}

export function statusLabel(status) {
  const key = `common.${status}`;
  const label = t(key);
  return label === key ? String(status || "—") : label;
}

export function statusPill(status, extra = "") {
  const normalized = status || "unknown";
  return `<span class="status-pill ${extra}" data-status="${attr(normalized)}">${esc(
    statusLabel(normalized),
  )}</span>`;
}

export function progressBar(progress, status = "", stage = "") {
  const value = Math.max(0, Math.min(1, Number(progress) || 0));
  const stateClass =
    status === "ready"
      ? "is-ready"
      : status === "failed"
        ? "is-failed"
        : status === "processing"
          ? "is-processing"
          : "";
  return `<div class="progress-block">
    <div class="progress-label">
      <span>${esc(formatStage(stage) || statusLabel(status))}</span>
      <span dir="ltr">${formatPercent(value)}</span>
    </div>
    <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(
      value * 100,
    )}" aria-label="${attr(statusLabel(status))}">
      <div class="progress-fill ${stateClass}" style="width: ${value * 100}%"></div>
    </div>
  </div>`;
}

export function jobTitle(job) {
  if (!job) return t("common.unknown");
  return (
    job.original_filename ||
    job.source_value ||
    job.metadata?.format_name ||
    job.id ||
    t("library.noSource")
  );
}

export function jobSubtitle(job) {
  if (!job) return "";
  const parts = [];
  if (job.metadata?.duration_seconds) parts.push(formatDuration(job.metadata.duration_seconds));
  if (job.metadata?.video_streams?.[0]) {
    const stream = job.metadata.video_streams[0];
    if (stream.width && stream.height) parts.push(`${stream.width}×${stream.height}`);
  }
  if (job.source_type) parts.push(job.source_type);
  parts.push(job.id?.slice(0, 8) || "—");
  return parts.join(" · ");
}

export function jobStatus(job) {
  return job?.status || "queued";
}

export function renderNotice(message, type = "info", title = "") {
  const iconName = type === "error" ? "alert" : type === "warning" ? "alert" : "info";
  return `<div class="notice ${type === "warning" ? "is-warning" : type === "error" ? "is-error" : ""}">
    ${icon(iconName)}
    <div>${title ? `<strong>${esc(title)}</strong> ` : ""}${message}</div>
  </div>`;
}

export function renderEmpty({ iconName = "film", title, body, action = "" }) {
  return `<div class="empty-state">
    <span class="empty-icon">${icon(iconName)}</span>
    <h3>${esc(title)}</h3>
    <p>${esc(body)}</p>
    ${action}
  </div>`;
}

export function renderSkeletonRows(count = 5) {
  return Array.from({ length: count })
    .map(
      () => `<div class="job-row" aria-hidden="true">
        <div class="job-row-main">
          <span class="skeleton" style="display:block;width:55%;height:12px"></span>
          <span class="skeleton" style="display:block;width:32%;height:9px;margin-top:8px"></span>
        </div>
        <span class="skeleton" style="display:block;width:56px;height:10px"></span>
        <span class="skeleton" style="display:block;width:68px;height:20px"></span>
        <span class="skeleton" style="display:block;width:80%;height:8px"></span>
        <span></span>
      </div>`,
    )
    .join("");
}

export function toast(message, type = "info", timeout = 4200) {
  const region = document.getElementById("toast-region");
  if (!region) return;
  const node = document.createElement("div");
  node.className = `toast ${type === "success" ? "is-success" : type === "error" ? "is-error" : type === "warning" ? "is-warning" : ""}`;
  const iconName = type === "error" || type === "warning" ? "alert" : "info";
  node.innerHTML = `${icon(iconName)}<span>${esc(message)}</span>`;
  region.append(node);
  window.setTimeout(() => {
    node.style.opacity = "0";
    node.style.transform = "translateY(6px)";
    window.setTimeout(() => node.remove(), 220);
  }, timeout);
}

export function setBusy(button, busy, label = "") {
  if (!button) return;
  if (busy) {
    button.dataset.originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = `${icon("refresh", "spin")}<span>${esc(label || t("common.loading"))}</span>`;
  } else {
    button.disabled = false;
    if (button.dataset.originalHtml) {
      button.innerHTML = button.dataset.originalHtml;
      delete button.dataset.originalHtml;
    }
  }
}

export function dialogElement() {
  return document.getElementById("studio-dialog");
}

export function openDialog({ title, subtitle = "", body, footer = "", onMount, className = "" }) {
  const dialog = dialogElement();
  if (!dialog) return null;
  dialog.className = `studio-dialog ${className}`;
  dialog.innerHTML = `<div class="dialog-head">
      <div>
        <h2 id="dialog-title">${esc(title)}</h2>
        ${subtitle ? `<p>${esc(subtitle)}</p>` : ""}
      </div>
      <button class="icon-button" type="button" data-close-dialog aria-label="${attr(t("common.close"))}">
        ${icon("cancel")}
      </button>
    </div>
    <div class="dialog-body">${body}</div>
    ${footer ? `<div class="dialog-actions">${footer}</div>` : ""}`;
  dialog.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", closeDialog);
  });
  dialog.oncancel = (event) => {
    event.preventDefault();
    closeDialog();
  };
  if (onMount) onMount(dialog);
  if (!dialog.open) dialog.showModal();
  return dialog;
}

export function closeDialog() {
  const dialog = dialogElement();
  if (dialog?.open) dialog.close();
  if (dialog) dialog.innerHTML = "";
}

export function confirmDialog(message, { title = "", confirmLabel = "", danger = false } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      closeDialog();
      resolve(value);
    };
    openDialog({
      title: title || t("common.warning"),
      body: `<p class="muted" style="font-size:13px;line-height:1.6">${esc(message)}</p>`,
      footer: `<button class="button button-secondary" type="button" data-confirm-no>${esc(
        t("common.cancel"),
      )}</button>
        <button class="button ${danger ? "button-danger" : "button-primary"}" type="button" data-confirm-yes>${esc(
          confirmLabel || t("common.open"),
        )}</button>`,
      onMount(dialog) {
        dialog.querySelector("[data-confirm-no]")?.addEventListener("click", () => finish(false));
        dialog.querySelector("[data-confirm-yes]")?.addEventListener("click", () => finish(true));
        dialog.addEventListener("close", () => finish(false), { once: true });
      },
    });
  });
}

export async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

export function downloadText(filename, text) {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function formatDateTimeLabel(value) {
  return formatDateTime(value);
}

export function formatRelativeLabel(value) {
  return formatRelative(value);
}

export function formatNumberLabel(value) {
  return formatNumber(value);
}

export function renderError(error, retry = "") {
  const isBackend = error?.status === 0 || error?.category === "provider_unavailable";
  const isNotFound = error?.status === 404 || error?.category === "not_found";
  const title = isBackend ? t("error.backend") : isNotFound ? t("error.notFound") : t("error.title");
  const body = isBackend
    ? t("error.backendBody")
    : isNotFound
      ? t("error.notFoundBody")
      : error?.message || t("error.genericBody");
  return `<div class="error-state page-enter">
    ${icon("alert")}
    <h1>${esc(title)}</h1>
    <p>${esc(body)}</p>
    ${isBackend ? `<p><code>uv run clipping-ai serve</code></p>` : ""}
    <div class="page-actions">
      ${retry}
      <a class="button button-secondary" href="#/library">${esc(t("error.goLibrary"))}</a>
    </div>
  </div>`;
}

export function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
