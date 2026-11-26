// Pipeline page: daily review, Google Sheet source, and the queue of links from the sheet.
// The review only suggests. Nothing here uploads or schedules; sending a link to clipping is
// one explicit click per link, and uploads stay in the YouTube panel on the clip page.
import { API_BASE } from "./api.js";
import { getLocale } from "./i18n.js";
import * as ui from "./ui.js";

const TEXT = {
  en: {
    nav: "Pipeline",
    title: "Content pipeline",
    description: "Links from your Google Sheet, stored on this computer, and a daily check of what to do next.",
    review: "Daily review",
    reviewHint: "What needs a decision today. These are suggestions only. Nothing runs by itself.",
    week: "Next 7 days: scheduled releases",
    none: "Nothing to review. All good.",
    source: "Google Sheet",
    sourceHint:
      "Paste the link of a sheet shared as 'Anyone with the link can view'. A Google Form responses sheet works too. The tool reads the first link it finds in each row.",
    sheetUrl: "Sheet link",
    save: "Save link",
    sync: "Sync now",
    lastSync: "Last sync: {time}",
    neverSynced: "Not synced yet.",
    synced: "Sync done. {n} new link(s).",
    saved: "Sheet link saved.",
    queue: "Queue",
    queueHint: "Every link from the sheet. Send a link to clipping when you want it processed.",
    empty: "No links yet. Save the sheet link and press Sync.",
    colLink: "Link",
    colStatus: "Status",
    send: "Send to clipping",
    skip: "Skip",
    restore: "Restore",
    openJob: "Open job",
    sentOk: "Sent to clipping.",
    sentAlready: "This link was already in your library; the existing job was used.",
    st_new: "New",
    st_sent: "Sent",
    st_skipped: "Skipped",
    reload: "Reload",
    "tip.no_sheet": "No sheet connected yet. Add your Google Sheet link below.",
    "tip.sync_error": "The last sync failed. See the message under Google Sheet.",
    "tip.stale_sync": "The sheet was not synced in the last 24 hours. Press Sync now.",
    "tip.new_links": "{n} new link(s) are waiting in the queue. Send the ones you want to clipping.",
    "tip.queue_empty": "The queue is empty. Add new links to the sheet to keep the channel fed.",
    "tip.ready_clips": "{n} ready clip(s) are not uploaded yet. Open a clip to upload or schedule it.",
    "tip.failed_clips": "{n} clip(s) failed. Check the Clip library.",
    "tip.nothing_scheduled": "Nothing is scheduled for the next 7 days.",
    "tip.empty_days": "{n} of the next 7 days have no scheduled release.",
  },
  ar: {
    nav: "خط الإنتاج",
    title: "خط إنتاج المحتوى",
    description: "روابط من Google Sheet محفوظة على جهازك، ومراجعة يومية لما يجب فعله بعد ذلك.",
    review: "المراجعة اليومية",
    reviewHint: "ما يحتاج قرارًا اليوم. هذه اقتراحات فقط ولا يعمل شيء تلقائيًا.",
    week: "الأيام السبعة القادمة: مواعيد النشر",
    none: "لا شيء للمراجعة. كل شيء تمام.",
    source: "Google Sheet",
    sourceHint:
      "الصق رابط شيت مشارك بخيار 'أي شخص معه الرابط يمكنه العرض'. شيت ردود Google Form يعمل أيضًا. تقرأ الأداة أول رابط في كل صف.",
    sheetUrl: "رابط الشيت",
    save: "حفظ الرابط",
    sync: "مزامنة الآن",
    lastSync: "آخر مزامنة: {time}",
    neverSynced: "لم تتم مزامنة بعد.",
    synced: "تمت المزامنة. {n} رابط جديد.",
    saved: "تم حفظ رابط الشيت.",
    queue: "الطابور",
    queueHint: "كل الروابط من الشيت. أرسل الرابط إلى القص عندما تريد معالجته.",
    empty: "لا توجد روابط بعد. احفظ رابط الشيت ثم اضغط مزامنة.",
    colLink: "الرابط",
    colStatus: "الحالة",
    send: "إرسال إلى القص",
    skip: "تخطي",
    restore: "استعادة",
    openJob: "فتح المهمة",
    sentOk: "تم الإرسال إلى القص.",
    sentAlready: "هذا الرابط موجود في مكتبتك بالفعل، وتم استخدام المهمة الموجودة.",
    st_new: "جديد",
    st_sent: "تم الإرسال",
    st_skipped: "تم التخطي",
    reload: "تحديث",
    "tip.no_sheet": "لم يتم ربط شيت بعد. أضف رابط Google Sheet بالأسفل.",
    "tip.sync_error": "فشلت آخر مزامنة. راجع الرسالة تحت Google Sheet.",
    "tip.stale_sync": "لم تتم مزامنة الشيت خلال آخر 24 ساعة. اضغط مزامنة الآن.",
    "tip.new_links": "{n} رابط جديد ينتظر في الطابور. أرسل ما تريده إلى القص.",
    "tip.queue_empty": "الطابور فارغ. أضف روابط جديدة إلى الشيت ليستمر ضخ المحتوى.",
    "tip.ready_clips": "{n} كليب جاهز لم يُرفع بعد. افتح الكليب لرفعه أو جدولته.",
    "tip.failed_clips": "فشل {n} كليب. راجع مكتبة الكليبات.",
    "tip.nothing_scheduled": "لا يوجد شيء مجدول خلال الأيام السبعة القادمة.",
    "tip.empty_days": "{n} من الأيام السبعة القادمة بلا موعد نشر.",
  },
};

export function pipelineText(key, params = {}) {
  const table = TEXT[getLocale()] || TEXT.en;
  const template = table[key] ?? TEXT.en[key] ?? key;
  return String(template).replace(/\{(\w+)\}/g, (_, name) =>
    params[name] === undefined ? `{${name}}` : String(params[name]),
  );
}
const tr = pipelineText;

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

function tipHtml(tip) {
  const type = tip.level === "warn" ? "warning" : "info";
  return ui.renderNotice(ui.esc(tr(`tip.${tip.kind}`, { n: tip.count })), type);
}

function reviewHtml(review) {
  const tips = review.tips.length
    ? review.tips.map(tipHtml).join("")
    : `<p class="field-hint">${ui.esc(tr("none"))}</p>`;
  const days = review.schedule
    .map((day) => {
      const label = new Date(`${day.date}T12:00:00`).toLocaleDateString(getLocale(), {
        weekday: "short",
        day: "numeric",
      });
      return `<div class="pl-day ${day.count ? "has-items" : ""}"><span>${ui.esc(label)}</span><strong>${day.count}</strong></div>`;
    })
    .join("");
  return `${tips}<h4 class="pl-sub">${ui.esc(tr("week"))}</h4><div class="pl-week">${days}</div>`;
}

function sourceHtml(source) {
  const sync = source.last_sync_at
    ? tr("lastSync", { time: ui.formatDateTimeLabel(source.last_sync_at) })
    : tr("neverSynced");
  const error = source.last_error ? ui.renderNotice(ui.esc(source.last_error), "error") : "";
  return `<div class="field">
      <label for="pl-sheet-url">${ui.esc(tr("sheetUrl"))}</label>
      <input type="url" id="pl-sheet-url" dir="ltr" placeholder="https://docs.google.com/spreadsheets/d/…" value="${ui.attr(source.sheet_url || "")}" />
    </div>
    ${error}
    <p class="field-hint">${ui.esc(sync)}</p>
    <div class="form-actions">
      <button class="button button-secondary" type="button" id="pl-save">${ui.esc(tr("save"))}</button>
      <button class="button button-primary" type="button" id="pl-sync" ${source.sheet_url ? "" : "disabled"}>${ui.icon("refresh")}<span>${ui.esc(tr("sync"))}</span></button>
    </div>`;
}

function itemHtml(item) {
  const title = item.title ? `<strong>${ui.esc(item.title)}</strong><br />` : "";
  const note = item.notes ? `<small class="muted"> ${ui.esc(item.notes)}</small>` : "";
  const buttons =
    item.status === "new"
      ? `<button class="button button-primary button-small" type="button" data-pl="send" data-id="${item.id}">${ui.esc(tr("send"))}</button>
         <button class="button button-ghost button-small" type="button" data-pl="skip" data-id="${item.id}">${ui.esc(tr("skip"))}</button>`
      : item.status === "skipped"
        ? `<button class="button button-ghost button-small" type="button" data-pl="restore" data-id="${item.id}">${ui.esc(tr("restore"))}</button>`
        : item.job_id
          ? `<a class="button button-secondary button-small" href="#/video/${ui.attr(item.job_id)}">${ui.esc(tr("openJob"))}</a>`
          : "";
  return `<div class="pl-row">
    <div class="pl-link">${title}<a href="${ui.attr(item.url)}" target="_blank" rel="noopener noreferrer" dir="ltr">${ui.esc(item.url)}</a>${note}</div>
    <span class="pl-status is-${ui.attr(item.status)}">${ui.esc(tr(`st_${item.status}`))}</span>
    <div class="pl-actions">${buttons}</div>
  </div>`;
}

function queueHtml(items) {
  if (!items.length) return `<p class="field-hint">${ui.esc(tr("empty"))}</p>`;
  return items.map(itemHtml).join("");
}

export async function pipelineView(token, isCurrent = () => true) {
  const [source, items, review] = await Promise.all([
    call("/api/pipeline/source"),
    call("/api/pipeline/items"),
    call("/api/pipeline/review"),
  ]);
  const html = `<div class="page-enter pl-page">
    <div class="page-head"><div class="page-head-copy">
      <div class="page-context">${ui.esc(tr("nav"))}</div>
      <h1>${ui.esc(tr("title"))}</h1>
      <p class="page-description">${ui.esc(tr("description"))}</p>
    </div></div>
    <section class="form-section" id="pl-review">
      <div class="form-section-header"><div><h3>${ui.esc(tr("review"))}</h3><p>${ui.esc(tr("reviewHint"))}</p></div></div>
      <div id="pl-review-body">${reviewHtml(review)}</div>
    </section>
    <section class="form-section" id="pl-source">
      <div class="form-section-header"><div><h3>${ui.esc(tr("source"))}</h3><p>${ui.esc(tr("sourceHint"))}</p></div></div>
      <div id="pl-source-body">${sourceHtml(source)}</div>
    </section>
    <section class="form-section" id="pl-queue">
      <div class="form-section-header"><div><h3>${ui.esc(tr("queue"))}</h3><p>${ui.esc(tr("queueHint"))}</p></div></div>
      <div id="pl-queue-body" class="pl-queue">${queueHtml(items.items)}</div>
    </section>
  </div>`;

  async function refresh(root) {
    const [s, i, r] = await Promise.all([
      call("/api/pipeline/source"),
      call("/api/pipeline/items"),
      call("/api/pipeline/review"),
    ]);
    if (!isCurrent()) return;
    root.querySelector("#pl-source-body").innerHTML = sourceHtml(s);
    root.querySelector("#pl-queue-body").innerHTML = queueHtml(i.items);
    root.querySelector("#pl-review-body").innerHTML = reviewHtml(r);
  }

  function mount() {
    const root = document.getElementById("pl-review")?.closest(".pl-page");
    if (!root) return;
    const run = async (button, work) => {
      if (button.disabled) return;
      button.disabled = true;
      try {
        await work();
      } catch (error) {
        ui.toast(error.message, "error");
      } finally {
        try {
          await refresh(root);
        } catch {
          button.disabled = false;
        }
      }
    };
    root.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      if (button.id === "pl-save") {
        const value = root.querySelector("#pl-sheet-url").value.trim();
        run(button, async () => {
          await call("/api/pipeline/source", { method: "PUT", body: { sheet_url: value } });
          ui.toast(tr("saved"), "success");
        });
      } else if (button.id === "pl-sync") {
        run(button, async () => {
          const result = await call("/api/pipeline/sync", { method: "POST" });
          ui.toast(tr("synced", { n: result.added }), "success");
        });
      } else if (button.dataset.pl) {
        const id = button.dataset.id;
        run(button, async () => {
          const result = await call(`/api/pipeline/items/${id}/${button.dataset.pl}`, {
            method: "POST",
          });
          if (button.dataset.pl === "send") {
            ui.toast(tr(result.duplicate ? "sentAlready" : "sentOk"), "success");
          }
        });
      }
    });
  }

  return { title: tr("title"), html, mount };
}
