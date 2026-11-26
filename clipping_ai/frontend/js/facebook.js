// Facebook Page publishing (Reels): a Connections section on the Pipeline page and a
// "Post to Facebook" panel on the clip page. Nothing is posted without a confirmation that
// names the Page and the time. The UI never receives or shows any token or the app secret.
import { API_BASE } from "./api.js";
import { getLocale } from "./i18n.js";
import * as ui from "./ui.js";

const MIN_LEAD_MS = 10 * 60 * 1000;
const POLL_MS = 5000;
const sending = new Set();

const TEXT = {
  en: {
    title: "Facebook",
    hint: "Connect your Facebook Page to post clips as Reels. Facebook Reels go to a Page, not a personal profile. Tokens stay on this computer.",
    loading: "Checking Facebook connection…",
    noClient: "Add FACEBOOK_APP_ID and FACEBOOK_APP_SECRET to .env and restart the server. See FACEBOOK_SETUP.md.",
    redirect: "Add this exact address to your Meta app as a Valid OAuth Redirect URI: {uri}",
    notConnected: "Not connected",
    connected: "Connected",
    connect: "Connect",
    reconnect: "Reconnect",
    disconnect: "Disconnect",
    disconnected: "Disconnected on this computer. You can also remove the app in your Facebook settings.",
    page: "Page to post on",
    pickPage: "Choose a Page…",
    refreshPages: "Refresh Pages",
    noPages: "Facebook returned no Pages for this account. You need to be an admin of a Page, and to have chosen it when you signed in.",
    cannotPost: "no permission to post",
    used: "Posted by this app in the last 24 hours: {n} of {max} (Facebook's limit per Page).",
    connectedOk: "Facebook connected.",
    connectNoPages: "Facebook connected, but no Pages were found for this account.",
    connectDenied: "Facebook sign-in was cancelled. Nothing was saved.",
    connectFailed: "Facebook sign-in failed. Nothing was saved. Try again.",
    pagesRefreshed: "Pages refreshed.",
    panelHint: "Posts the exported clip to your Facebook Page as a Reel. It uses the clip's saved description and hashtags.",
    panelNotReady: "Facebook is not ready. Connect it and choose a Page under Connections on the Pipeline page.",
    panelOn: "Posting to {page}",
    when: "When",
    now: "Post now",
    schedule: "Schedule on Facebook",
    scheduleHint: "Facebook publishes it at this time. Your time zone is {zone}. Must be at least 10 minutes from now.",
    scheduleAt: "Publish time",
    post: "Post to Facebook",
    posting: "Posting… keep this page open",
    inProgress: "A post of this clip is running. This page checks again every few seconds.",
    done: "Sent to Facebook",
    doneNow: "Posted to {page}.",
    doneScheduled: "Scheduled on {page} for {time}.",
    already: "This clip is already on Facebook. It is not sent twice.",
    confirmTitle: "Post this Reel?",
    confirmNow: "This will publish the Reel on the Page \"{page}\" right now.",
    confirmSchedule: "This will schedule the Reel on the Page \"{page}\" for {time}.",
    confirmYes: "Post",
    needTime: "Pick a publish time.",
    badTime: "The publish time must be at least 10 minutes from now.",
    failed: "Posting failed",
    statusFailed: "Could not read the Facebook status.",
    success: "Sent to Facebook.",
  },
  ar: {
    title: "فيسبوك",
    hint: "اربط صفحة فيسبوك لنشر الكليبات كـ Reels. الريلز تنزل على Page وليس على البروفايل الشخصي. التوكن يبقى على هذا الجهاز.",
    loading: "جارٍ فحص الاتصال بفيسبوك…",
    noClient: "أضف FACEBOOK_APP_ID وFACEBOOK_APP_SECRET في ملف .env وأعد تشغيل الخادم. راجع FACEBOOK_SETUP.md.",
    redirect: "أضف هذا العنوان بالضبط في تطبيق Meta كـ Valid OAuth Redirect URI: {uri}",
    notConnected: "غير مربوط",
    connected: "مربوط",
    connect: "ربط",
    reconnect: "إعادة الربط",
    disconnect: "فصل",
    disconnected: "تم الفصل على هذا الجهاز. يمكنك أيضًا إزالة التطبيق من إعدادات فيسبوك.",
    page: "الصفحة المراد النشر عليها",
    pickPage: "اختر صفحة…",
    refreshPages: "تحديث الصفحات",
    noPages: "لم يرجع فيسبوك أي صفحات لهذا الحساب. لازم تكون أدمن على Page وتختارها وقت تسجيل الدخول.",
    cannotPost: "لا توجد صلاحية نشر",
    used: "تم النشر من هذا التطبيق في آخر 24 ساعة: {n} من {max} (حد فيسبوك لكل صفحة).",
    connectedOk: "تم ربط فيسبوك.",
    connectNoPages: "تم ربط فيسبوك لكن لم يتم العثور على صفحات لهذا الحساب.",
    connectDenied: "تم إلغاء تسجيل الدخول إلى فيسبوك. لم يُحفظ شيء.",
    connectFailed: "فشل تسجيل الدخول إلى فيسبوك. لم يُحفظ شيء. حاول مرة أخرى.",
    pagesRefreshed: "تم تحديث الصفحات.",
    panelHint: "ينشر الكليب المُصدَّر على صفحتك في فيسبوك كـ Reel، ويستخدم الوصف والهاشتاجات المحفوظة للكليب.",
    panelNotReady: "فيسبوك غير جاهز. اربطه واختر صفحة من الربط في صفحة Pipeline.",
    panelOn: "النشر على {page}",
    when: "الوقت",
    now: "انشر الآن",
    schedule: "جدولة على فيسبوك",
    scheduleHint: "ينشره فيسبوك في هذا الوقت. منطقتك الزمنية {zone}. يجب أن يكون بعد 10 دقائق على الأقل من الآن.",
    scheduleAt: "وقت النشر",
    post: "نشر على فيسبوك",
    posting: "جارٍ النشر… اترك الصفحة مفتوحة",
    inProgress: "يوجد نشر جارٍ لهذا الكليب. تتحقق الصفحة مرة أخرى كل بضع ثوانٍ.",
    done: "تم الإرسال إلى فيسبوك",
    doneNow: "تم النشر على {page}.",
    doneScheduled: "تمت الجدولة على {page} في {time}.",
    already: "هذا الكليب موجود على فيسبوك بالفعل ولن يُرسل مرتين.",
    confirmTitle: "نشر هذا الـ Reel؟",
    confirmNow: "سيتم نشر الـ Reel على الصفحة \"{page}\" الآن.",
    confirmSchedule: "سيتم جدولة الـ Reel على الصفحة \"{page}\" في {time}.",
    confirmYes: "انشر",
    needTime: "اختر وقت النشر.",
    badTime: "وقت النشر يجب أن يكون بعد 10 دقائق على الأقل من الآن.",
    failed: "فشل النشر",
    statusFailed: "تعذر قراءة حالة فيسبوك.",
    success: "تم الإرسال إلى فيسبوك.",
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

// "2026-12-01T18:00" (datetime-local) -> "2026-12-01T18:00:00+02:00"
function toPublishAt(localValue) {
  if (!localValue) return null;
  const date = new Date(localValue);
  if (Number.isNaN(date.getTime())) return null;
  const minutes = -date.getTimezoneOffset();
  const sign = minutes >= 0 ? "+" : "-";
  const abs = Math.abs(minutes);
  const offset = `${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
  const withSeconds = localValue.length === 16 ? `${localValue}:00` : localValue;
  return { iso: `${withSeconds}${offset}`, date };
}

// ---- Connections section (Pipeline page) -------------------------------------------------

export function facebookSectionHtml() {
  return `<section class="form-section" id="pl-facebook">
    <div class="form-section-header"><div><h3>${ui.esc(tr("title"))}</h3><p>${ui.esc(tr("hint"))}</p></div></div>
    <div id="pl-facebook-body"><p class="field-hint">${ui.esc(tr("loading"))}</p></div>
  </section>`;
}

function connectionHtml(status) {
  if (!status.client_configured) return ui.renderNotice(ui.esc(tr("noClient")), "warning");
  const hint = `<p class="field-hint" dir="ltr">${ui.esc(tr("redirect", { uri: status.redirect_uri }))}</p>`;
  const state = status.connected ? tr("connected") : tr("notConnected");
  const actions = status.connected
    ? `<button class="button button-secondary button-small" type="button" data-fb-connect>${ui.esc(tr("reconnect"))}</button>
       <button class="button button-ghost button-small" type="button" data-fb-disconnect>${ui.esc(tr("disconnect"))}</button>`
    : `<button class="button button-primary button-small" type="button" data-fb-connect>${ui.esc(tr("connect"))}</button>`;
  let body = `${hint}<div class="pl-row"><div class="pl-link"><strong>${ui.esc(tr("title"))}</strong></div>
    <span class="pl-status ${status.connected ? "is-sent" : "is-new"}">${ui.esc(state)}</span>
    <div class="pl-actions">${actions}</div></div>`;
  if (status.connected) {
    if (!status.pages.length) {
      body += ui.renderNotice(ui.esc(tr("noPages")), "warning");
    } else {
      const options = status.pages
        .map((p) => {
          const label = p.can_post ? p.name : `${p.name} (${tr("cannotPost")})`;
          const selected = status.selected_page && status.selected_page.id === p.id ? " selected" : "";
          return `<option value="${ui.attr(p.id)}"${selected}>${ui.esc(label)}</option>`;
        })
        .join("");
      body += `<div class="field"><label for="fb-page">${ui.esc(tr("page"))}</label>
        <select id="fb-page"><option value=""${status.selected_page ? "" : " selected"}>${ui.esc(tr("pickPage"))}</option>${options}</select></div>`;
    }
    body += `<div class="form-actions"><button class="button button-ghost button-small" type="button" data-fb-refresh>${ui.esc(tr("refreshPages"))}</button></div>`;
    if (status.selected_page) {
      body += `<p class="field-hint">${ui.esc(tr("used", { n: status.used_24h, max: status.daily_limit }))}</p>`;
    }
  }
  return body;
}

export function mountFacebookSection(root) {
  const section = root.querySelector("#pl-facebook");
  const body = section?.querySelector("#pl-facebook-body");
  if (!section || !body) return;

  const hash = window.location.hash || "";
  const query = new URLSearchParams(hash.includes("?") ? hash.split("?")[1] : "");
  if (query.has("fb")) {
    const outcome = query.get("fb");
    if (outcome === "ok") ui.toast(tr("connectedOk"), "success");
    else if (outcome === "nopages") ui.toast(tr("connectNoPages"), "warning");
    else ui.toast(outcome === "denied" ? tr("connectDenied") : tr("connectFailed"), "error");
    try {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#/pipeline`);
    } catch {
      /* the message was shown */
    }
  }

  async function render() {
    try {
      const status = await call("/api/facebook/status");
      if (section.isConnected) body.innerHTML = connectionHtml(status);
    } catch (error) {
      if (section.isConnected) body.innerHTML = ui.renderNotice(ui.esc(error.message || tr("statusFailed")), "error");
    }
  }

  const run = async (button, work) => {
    if (button.disabled) return;
    button.disabled = true;
    try {
      await work();
    } catch (error) {
      ui.toast(error.message, "error");
    } finally {
      await render();
    }
  };

  section.addEventListener("change", (event) => {
    if (event.target.id !== "fb-page" || !event.target.value) return;
    call("/api/facebook/page", { method: "PUT", body: { page_id: event.target.value } })
      .then(render)
      .catch((error) => ui.toast(error.message, "error"));
  });
  section.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if ("fbConnect" in button.dataset) {
      run(button, async () => {
        const out = await call("/api/facebook/connect", {
          method: "POST",
          body: { return_to: window.location.href.split("#")[0] },
        });
        window.location.assign(out.url);
        await new Promise(() => {});
      });
    } else if ("fbDisconnect" in button.dataset) {
      run(button, async () => {
        await call("/api/facebook/disconnect", { method: "POST" });
        ui.toast(tr("disconnected"), "success");
      });
    } else if ("fbRefresh" in button.dataset) {
      run(button, async () => {
        await call("/api/facebook/pages/refresh", { method: "POST" });
        ui.toast(tr("pagesRefreshed"), "success");
      });
    }
  });
  render();
}

// ---- "Post to Facebook" panel (clip page) ---------------------------------------------------

export function facebookPanelHtml(clip) {
  return `<section class="form-section yt-panel fb-panel" id="fb-panel" data-clip-id="${ui.attr(clip.id)}">
    <div class="form-section-header">
      <div><h3>${ui.esc(tr("title"))}</h3><p>${ui.esc(tr("panelHint"))}</p></div>
    </div>
    <div id="fb-body" aria-live="polite"><p class="field-hint">${ui.esc(tr("loading"))}</p></div>
  </section>`;
}

function postedHtml(post) {
  const page = post.page_name || post.page_id || "";
  const text =
    post.mode === "schedule" && post.publish_at
      ? tr("doneScheduled", { page, time: ui.formatDateTimeLabel(post.publish_at) })
      : tr("doneNow", { page });
  return `${ui.renderNotice(ui.esc(text), "info", tr("done"))}<p class="field-hint">${ui.esc(tr("already"))}</p>`;
}

function formHtml(status, zone) {
  return `<p class="field-hint">${ui.icon("check")} ${ui.esc(tr("panelOn", { page: status.selected_page.name }))}</p>
    <div class="field">
      <label for="fb-mode">${ui.esc(tr("when"))}</label>
      <select id="fb-mode">
        <option value="now" selected>${ui.esc(tr("now"))}</option>
        <option value="schedule">${ui.esc(tr("schedule"))}</option>
      </select>
    </div>
    <div class="field" id="fb-schedule-field" hidden>
      <label for="fb-publish-at">${ui.esc(tr("scheduleAt"))}</label>
      <input type="datetime-local" id="fb-publish-at" />
      <small class="field-hint">${ui.esc(tr("scheduleHint", { zone }))}</small>
    </div>
    <p class="field-hint">${ui.esc(tr("used", { n: status.used_24h, max: status.daily_limit }))}</p>
    <div id="fb-message"></div>
    <div class="form-actions">
      <button class="button button-primary" type="button" id="fb-post">${ui.icon("upload")}<span>${ui.esc(tr("post"))}</span></button>
    </div>`;
}

export function mountFacebookPanel(root, clip) {
  const panel = root.querySelector("#fb-panel");
  const body = panel?.querySelector("#fb-body");
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
    const node = body.querySelector("#fb-message");
    if (node) node.innerHTML = html;
  };

  async function render() {
    if (!alive || !panel.isConnected) return stop();
    let status;
    let state;
    try {
      [status, state] = await Promise.all([call("/api/facebook/status"), call(`/api/facebook/clips/${enc}`)]);
    } catch (error) {
      body.innerHTML = ui.renderNotice(ui.esc(error.message || tr("statusFailed")), "error");
      return;
    }
    if (!panel.isConnected) return;
    if (state.post) {
      body.innerHTML = postedHtml(state.post);
      return;
    }
    if (state.in_progress || sending.has(clipId)) {
      body.innerHTML = ui.renderNotice(ui.esc(tr("inProgress")), "info", tr("posting"));
      stop();
      timer = window.setTimeout(render, POLL_MS);
      return;
    }
    if (!status.client_configured) {
      body.innerHTML = ui.renderNotice(ui.esc(tr("noClient")), "warning");
      return;
    }
    const page = status.selected_page && status.pages.find((p) => p.id === status.selected_page.id);
    if (!status.connected || !page || !page.can_post) {
      body.innerHTML = ui.renderNotice(ui.esc(tr("panelNotReady")), "warning");
      return;
    }
    body.innerHTML = formHtml(status, zone);
    wire(status);
  }

  function wire(status) {
    const mode = body.querySelector("#fb-mode");
    const field = body.querySelector("#fb-schedule-field");
    const when = body.querySelector("#fb-publish-at");
    const button = body.querySelector("#fb-post");
    mode.addEventListener("change", () => {
      field.hidden = mode.value !== "schedule";
    });
    button.addEventListener("click", async () => {
      if (sending.has(clipId)) return;
      message("");
      const payload = { mode: mode.value, confirm: true };
      let confirmText = tr("confirmNow", { page: status.selected_page.name });
      if (mode.value === "schedule") {
        const parsed = toPublishAt(when.value);
        if (!parsed) return message(ui.renderNotice(ui.esc(tr("needTime")), "warning"));
        if (parsed.date.getTime() < Date.now() + MIN_LEAD_MS) {
          return message(ui.renderNotice(ui.esc(tr("badTime")), "warning"));
        }
        payload.publish_at = parsed.iso;
        confirmText = tr("confirmSchedule", {
          page: status.selected_page.name,
          time: ui.formatDateTimeLabel(parsed.iso),
        });
      }
      const ok = await ui.confirmDialog(confirmText, { title: tr("confirmTitle"), confirmLabel: tr("confirmYes") });
      if (!ok) return;
      if (sending.has(clipId)) return;
      sending.add(clipId);
      ui.setBusy(button, true, tr("posting"));
      try {
        const result = await call(`/api/facebook/clips/${enc}/publish`, { method: "POST", body: payload });
        sending.delete(clipId);
        ui.toast(tr("success"), "success");
        if (panel.isConnected) body.innerHTML = postedHtml(result.post);
      } catch (error) {
        sending.delete(clipId);
        if (!panel.isConnected) return;
        ui.setBusy(button, false);
        message(ui.renderNotice(ui.esc(error.message || tr("failed")), "error", tr("failed")));
        try {
          const state = await call(`/api/facebook/clips/${enc}`);
          if (state.post) body.innerHTML = postedHtml(state.post);
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
