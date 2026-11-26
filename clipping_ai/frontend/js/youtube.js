// "Upload to YouTube" panel for the clip detail page.
// Safety rules match the CLI: private by default, schedule only for private uploads with an
// explicit UTC offset, public needs a confirmation, and an uploaded clip is never re-sent.
// The panel never receives or shows any token or client secret.
import { API_BASE } from "./api.js";
import { getLocale } from "./i18n.js";
import * as ui from "./ui.js";

const STUDIO_PREFIX = "https://studio.youtube.com/";
const MIN_LEAD_MS = 15 * 60 * 1000;
const POLL_MS = 5000;
const sending = new Set();

const TEXT = {
  en: {
    title: "YouTube",
    hint: "Uploads the exported clip to your channel. It uses the clip's saved title, description and hashtags.",
    loading: "Checking YouTube connection…",
    disabled:
      "The YouTube module is off. Set CLIPPING_AI_YOUTUBE_ENABLED=true in .env and restart the server. See YOUTUBE_SETUP.md.",
    noClient:
      "Add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET to .env and restart the server. See YOUTUBE_SETUP.md.",
    notAuthorised: "Not connected yet. Run this on the computer that runs the app, then reload:",
    connected: "Connected",
    privacy: "Privacy",
    private: "Private (only you)",
    unlisted: "Unlisted (anyone with the link)",
    public: "Public (everyone)",
    schedule: "Schedule release",
    scheduleHint:
      "The video is uploaded as private and YouTube makes it public at this time. Your time zone is {zone}.",
    scheduleOnlyPrivate: "Scheduling works with Private only.",
    scheduleAt: "Release time",
    upload: "Upload to YouTube",
    uploading: "Uploading… keep this page open",
    inProgress: "An upload of this clip is running. This page checks again every few seconds.",
    done: "Uploaded",
    doneAs: "Uploaded as {privacy}.",
    scheduledFor: "Goes public at {time}.",
    openStudio: "Open in YouTube Studio",
    already: "This clip is already on YouTube. It is not uploaded twice.",
    confirmTitle: "Make this video public?",
    confirmBody:
      "Everyone will be able to watch it on your channel as soon as the upload finishes. Choose Private if you are not sure.",
    confirmYes: "Upload as public",
    needTime: "Pick a release time.",
    badTime: "Release time must be at least 15 minutes from now.",
    failed: "Upload failed",
    statusFailed: "Could not read the YouTube status.",
    success: "Uploaded to YouTube.",
    manual: "The clip's manual published flag is not changed.",
  },
  ar: {
    title: "يوتيوب",
    hint: "يرفع الكليب المُصدَّر إلى قناتك، ويستخدم العنوان والوصف والهاشتاجات المحفوظة للكليب.",
    loading: "جارٍ فحص الاتصال بيوتيوب…",
    disabled:
      "وحدة يوتيوب متوقفة. فعّل CLIPPING_AI_YOUTUBE_ENABLED=true في ملف .env وأعد تشغيل الخادم. راجع YOUTUBE_SETUP.md.",
    noClient:
      "أضف YOUTUBE_CLIENT_ID وYOUTUBE_CLIENT_SECRET في ملف .env وأعد تشغيل الخادم. راجع YOUTUBE_SETUP.md.",
    notAuthorised: "لم يتم الربط بعد. شغّل الأمر ده على الجهاز اللي بيشغل التطبيق ثم حدّث الصفحة:",
    connected: "متصل",
    privacy: "الخصوصية",
    private: "خاص (أنت فقط)",
    unlisted: "غير مدرج (لأي شخص معه الرابط)",
    public: "عام (للجميع)",
    schedule: "جدولة النشر",
    scheduleHint:
      "يُرفع الفيديو كخاص ويجعله يوتيوب عامًا في هذا الوقت. منطقتك الزمنية {zone}.",
    scheduleOnlyPrivate: "الجدولة متاحة مع الخاص فقط.",
    scheduleAt: "وقت النشر",
    upload: "رفع إلى يوتيوب",
    uploading: "جارٍ الرفع… اترك الصفحة مفتوحة",
    inProgress: "يوجد رفع جارٍ لهذا الكليب. تتحقق الصفحة مرة أخرى كل بضع ثوانٍ.",
    done: "تم الرفع",
    doneAs: "تم الرفع بوضع {privacy}.",
    scheduledFor: "سيصبح عامًا في {time}.",
    openStudio: "فتح في YouTube Studio",
    already: "هذا الكليب موجود على يوتيوب بالفعل ولن يُرفع مرتين.",
    confirmTitle: "جعل هذا الفيديو عامًا؟",
    confirmBody:
      "سيتمكن الجميع من مشاهدته على قناتك فور انتهاء الرفع. اختر خاص إذا لم تكن متأكدًا.",
    confirmYes: "رفع كعام",
    needTime: "اختر وقت النشر.",
    badTime: "وقت النشر يجب أن يكون بعد 15 دقيقة على الأقل من الآن.",
    failed: "فشل الرفع",
    statusFailed: "تعذر قراءة حالة يوتيوب.",
    success: "تم الرفع إلى يوتيوب.",
    manual: "علامة النشر اليدوي للكليب لم تتغير.",
  },
};

function tr(key, params = {}) {
  const table = TEXT[getLocale()] || TEXT.en;
  const template = table[key] ?? TEXT.en[key] ?? key;
  return String(template).replace(/\{(\w+)\}/g, (_, name) =>
    params[name] === undefined ? `{${name}}` : String(params[name]),
  );
}

async function call(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new Error("The local backend is not reachable. Start it with `uv run clipping-ai serve`.");
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const detail = payload?.detail;
    const message =
      payload?.error?.message ||
      (Array.isArray(detail) ? detail.map((item) => item.msg || String(item)).join("; ") : detail) ||
      `The request failed with status ${response.status}.`;
    throw new Error(String(message));
  }
  return payload;
}

export function offsetString(date) {
  const minutes = -date.getTimezoneOffset();
  const sign = minutes >= 0 ? "+" : "-";
  const abs = Math.abs(minutes);
  const hh = String(Math.floor(abs / 60)).padStart(2, "0");
  const mm = String(abs % 60).padStart(2, "0");
  return `${sign}${hh}:${mm}`;
}

// "2026-12-01T18:00" (from a datetime-local input) -> "2026-12-01T18:00:00+02:00"
export function toPublishAt(localValue) {
  if (!localValue) return null;
  const date = new Date(localValue);
  if (Number.isNaN(date.getTime())) return null;
  const withSeconds = localValue.length === 16 ? `${localValue}:00` : localValue;
  return { iso: `${withSeconds}${offsetString(date)}`, date };
}

export function youtubePanelHtml(clip) {
  return `<section class="form-section yt-panel" id="yt-panel" data-clip-id="${ui.attr(clip.id)}">
    <div class="form-section-header">
      <div><h3>${ui.esc(tr("title"))}</h3><p>${ui.esc(tr("hint"))}</p></div>
    </div>
    <div id="yt-body" aria-live="polite"><p class="field-hint">${ui.esc(tr("loading"))}</p></div>
  </section>`;
}

function uploadedHtml(upload) {
  const when = upload.publish_at ? ui.formatDateTimeLabel(upload.publish_at) : "";
  const link =
    upload.studio_url && upload.studio_url.startsWith(STUDIO_PREFIX)
      ? `<p><a class="text-link" href="${ui.attr(upload.studio_url)}" target="_blank" rel="noopener noreferrer">${ui.esc(
          tr("openStudio"),
        )}</a></p>`
      : "";
  return `${ui.renderNotice(
    `${ui.esc(tr("doneAs", { privacy: upload.privacy || "private" }))}${
      when ? ` ${ui.esc(tr("scheduledFor", { time: when }))}` : ""
    }`,
    "info",
    tr("done"),
  )}${link}<p class="field-hint">${ui.esc(tr("already"))} ${ui.esc(tr("manual"))}</p>`;
}

function formHtml(zone) {
  return `<p class="field-hint">${ui.icon("check")} ${ui.esc(tr("connected"))}</p>
    <div class="field">
      <label for="yt-privacy">${ui.esc(tr("privacy"))}</label>
      <select id="yt-privacy">
        <option value="private" selected>${ui.esc(tr("private"))}</option>
        <option value="unlisted">${ui.esc(tr("unlisted"))}</option>
        <option value="public">${ui.esc(tr("public"))}</option>
      </select>
    </div>
    <label class="check-row">
      <input type="checkbox" id="yt-schedule" />
      <span>${ui.esc(tr("schedule"))}<small id="yt-schedule-hint">${ui.esc(
        tr("scheduleHint", { zone }),
      )}</small></span>
    </label>
    <div class="field" id="yt-schedule-field" hidden>
      <label for="yt-publish-at">${ui.esc(tr("scheduleAt"))}</label>
      <input type="datetime-local" id="yt-publish-at" />
    </div>
    <div id="yt-message"></div>
    <div class="form-actions">
      <button class="button button-primary" type="button" id="yt-upload">${ui.icon(
        "upload",
      )}<span>${ui.esc(tr("upload"))}</span></button>
    </div>`;
}

export function mountYoutubePanel(root, clip) {
  const panel = root.querySelector("#yt-panel");
  const body = panel?.querySelector("#yt-body");
  if (!panel || !body) return;
  const clipId = clip.id;
  const enc = encodeURIComponent(clipId);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "local";
  let timer = null;
  let alive = true;

  const stop = () => {
    if (timer) window.clearTimeout(timer);
    timer = null;
  };

  const message = (html) => {
    const node = body.querySelector("#yt-message");
    if (node) node.innerHTML = html;
  };

  async function render() {
    if (!alive || !panel.isConnected) return stop();
    let status;
    let state;
    try {
      [status, state] = await Promise.all([
        call("/api/youtube/status"),
        call(`/api/youtube/clips/${enc}`),
      ]);
    } catch (error) {
      body.innerHTML = ui.renderNotice(ui.esc(error.message || tr("statusFailed")), "error");
      return;
    }
    if (!panel.isConnected) return;
    if (state.upload) {
      body.innerHTML = uploadedHtml(state.upload);
      return;
    }
    if (state.in_progress || sending.has(clipId)) {
      body.innerHTML = ui.renderNotice(ui.esc(tr("inProgress")), "info", tr("uploading"));
      stop();
      timer = window.setTimeout(render, POLL_MS);
      return;
    }
    if (!status.enabled) {
      body.innerHTML = ui.renderNotice(ui.esc(tr("disabled")), "warning");
      return;
    }
    if (!status.client_configured) {
      body.innerHTML = ui.renderNotice(ui.esc(tr("noClient")), "warning");
      return;
    }
    if (!status.authorised) {
      body.innerHTML = `${ui.renderNotice(ui.esc(tr("notAuthorised")), "warning")}<pre class="code-block" dir="ltr">uv run clipping-ai youtube auth</pre>`;
      return;
    }
    body.innerHTML = formHtml(zone);
    wire(status);
  }

  function wire(status) {
    const privacy = body.querySelector("#yt-privacy");
    const schedule = body.querySelector("#yt-schedule");
    const field = body.querySelector("#yt-schedule-field");
    const when = body.querySelector("#yt-publish-at");
    const hint = body.querySelector("#yt-schedule-hint");
    const button = body.querySelector("#yt-upload");
    privacy.value = status.default_privacy || "private";

    const sync = () => {
      const isPrivate = privacy.value === "private";
      if (!isPrivate) schedule.checked = false;
      schedule.disabled = !isPrivate;
      field.hidden = !schedule.checked;
      hint.textContent = isPrivate ? tr("scheduleHint", { zone }) : tr("scheduleOnlyPrivate");
    };
    privacy.addEventListener("change", sync);
    schedule.addEventListener("change", sync);
    sync();

    button.addEventListener("click", async () => {
      if (sending.has(clipId)) return;
      message("");
      const payload = { privacy: privacy.value };
      if (schedule.checked) {
        const parsed = toPublishAt(when.value);
        if (!parsed) return message(ui.renderNotice(ui.esc(tr("needTime")), "warning"));
        if (parsed.date.getTime() < Date.now() + MIN_LEAD_MS) {
          return message(ui.renderNotice(ui.esc(tr("badTime")), "warning"));
        }
        payload.publish_at = parsed.iso;
      }
      if (privacy.value === "public") {
        const ok = await ui.confirmDialog(tr("confirmBody"), {
          title: tr("confirmTitle"),
          confirmLabel: tr("confirmYes"),
          danger: true,
        });
        if (!ok) return;
        payload.confirm_public = true;
      }
      if (sending.has(clipId)) return;
      sending.add(clipId);
      ui.setBusy(button, true, tr("uploading"));
      try {
        const result = await call(`/api/youtube/clips/${enc}/upload`, {
          method: "POST",
          body: payload,
        });
        sending.delete(clipId);
        ui.toast(tr("success"), "success");
        if (panel.isConnected) body.innerHTML = uploadedHtml(result.upload);
      } catch (error) {
        sending.delete(clipId);
        if (!panel.isConnected) return;
        ui.setBusy(button, false);
        message(ui.renderNotice(ui.esc(error.message || tr("failed")), "error", tr("failed")));
        // The server may still hold or have finished this upload. Show its real state.
        try {
          const state = await call(`/api/youtube/clips/${enc}`);
          if (state.upload) body.innerHTML = uploadedHtml(state.upload);
          else if (state.in_progress) render();
        } catch {
          /* keep the error already shown */
        }
      }
    });
  }

  const observer = new MutationObserver(() => {
    if (!panel.isConnected) {
      alive = false;
      stop();
      observer.disconnect();
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
  render();
}
