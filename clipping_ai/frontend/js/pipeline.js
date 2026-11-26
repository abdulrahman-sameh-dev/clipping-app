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
      "Paste the normal link of your Google Sheet. It stays private: sign in once with Google (read-only) and nobody else can open it. A Google Form responses sheet works too. The tool reads the first link it finds in each row.",
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
    reused: "Already in your library",
    dupSynced: " {d} duplicate(s) ignored.",
    sentExisting: "This link is already in your library ({c} clip(s) exist). No new job was created.",
    access_login: "Private access: signed in with Google (read-only).",
    access_locked: "Locked: no sign-in yet, so nothing is read. Run `uv run clipping-ai pipeline auth sheets` on this computer.",
    access_public_link: "Public-link mode: the sheet must be shared with 'Anyone with the link'. Signing in is safer.",
    allowPublic: "Also allow sheets shared by link (not recommended)",
    clips: "Clips and auto-publish",
    clipsHint: "Every ready clip with its publish state. Auto-publish uploads the clips you confirm as private with a publish time. Nothing goes public immediately.",
    colClip: "Clip",
    colState: "Publish state",
    colAuto: "Skip auto-publish",
    noClips: "No ready clips yet.",
    s_not_uploaded: "Not uploaded",
    s_manual: "Marked published (manual)",
    s_scheduled: "Scheduled",
    s_went_live: "Went live (scheduled)",
    s_public: "Public",
    s_unlisted: "Unlisted",
    s_private: "Private (no date)",
    planBtn: "Plan auto-publish",
    planTitle: "Plan (nothing is uploaded yet)",
    planEmpty: "No clip is eligible. Ready, not uploaded and not excluded clips appear here.",
    planCap: "Showing the first {n} of {t} eligible clips (YouTube upload quota).",
    planNeedYt: "YouTube is not enabled or not signed in. Set CLIPPING_AI_YOUTUBE_ENABLED=true and run `uv run clipping-ai youtube auth`.",
    confirmRun: "Confirm: upload privately and schedule",
    planSettings: "Daily slot",
    hour: "Hour",
    minute: "Minute",
    perDay: "Per day",
    saveSettings: "Save slot",
    runDone: "Auto-publish finished: {ok} scheduled, {bad} not done.",
    r_scheduled: "Scheduled",
    r_failed: "Failed",
    r_skipped: "Skipped",
    analytics: "YouTube Analytics",
    analyticsHint: "Read-only numbers for the last 28 days. The suggestions below are rules, not AI, and nothing changes by itself.",
    refreshAnalytics: "Refresh analytics",
    analyticsOff: "Analytics is not connected. Run `uv run clipping-ai pipeline auth analytics` on this computer.",
    analyticsEmpty: "No uploaded videos yet.",
    colViews: "Views",
    colLikes: "Likes",
    colRetention: "Avg watched",
    colVerdict: "Verdict",
    median: "Median views of judged videos: {m}",
    v_strong: "Strong",
    v_weak: "Weak",
    v_ok: "OK",
    v_too_new: "Too new",
    v_not_live: "Not live yet",
    v_judge: "No data",
    refreshed: "Analytics refreshed.",
    "dec.need_more_data": "Not enough data yet: {n} video(s) are old enough to judge. At least 3 are needed.",
    "dec.make_more": "{n} clip(s) did much better than the rest. Make more content like them.",
    "dec.change_content": "{n} clip(s) did much worse than the rest. Review the title, hook or topic before posting similar content.",
    "dec.improve_hook": "{n} clip(s) lose viewers early (under 35% watched). Try a stronger first seconds.",
    "dec.all_weak": "All judged clips are weak. Consider changing the type of content.",
    "dec.keep_going": "No clear winner or loser. Keep the current plan.",
    "dec.no_numbers_yet": "{n} live clip(s) have no numbers yet. YouTube data can lag by a few days.",
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
      "الصق الرابط العادي لشيتك. يبقى خاصًا: سجّل الدخول مرة واحدة بحساب Google (قراءة فقط) ولا يستطيع أحد غيرك فتحه. شيت ردود Google Form يعمل أيضًا. تقرأ الأداة أول رابط في كل صف.",
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
    reused: "موجود بالفعل في مكتبتك",
    dupSynced: " تم تجاهل {d} رابط مكرر.",
    sentExisting: "هذا الرابط موجود بالفعل في مكتبتك (يوجد {c} كليب). لم يتم إنشاء مهمة جديدة.",
    access_login: "وصول خاص: تم تسجيل الدخول بحساب Google (قراءة فقط).",
    access_locked: "مقفل: لا يوجد تسجيل دخول بعد، فلا يُقرأ شيء. شغّل `uv run clipping-ai pipeline auth sheets` على هذا الجهاز.",
    access_public_link: "وضع الرابط العام: يجب مشاركة الشيت بخيار 'أي شخص معه الرابط'. تسجيل الدخول أكثر أمانًا.",
    allowPublic: "اسمح أيضًا بالشيتات المشاركة بالرابط (غير موصى به)",
    clips: "الكليبات والنشر التلقائي",
    clipsHint: "كل كليب جاهز مع حالة نشره. النشر التلقائي يرفع الكليبات التي تؤكدها كخاصة مع موعد نشر. لا شيء يصبح عامًا فورًا.",
    colClip: "الكليب",
    colState: "حالة النشر",
    colAuto: "استثناء من النشر التلقائي",
    noClips: "لا توجد كليبات جاهزة بعد.",
    s_not_uploaded: "لم يُرفع",
    s_manual: "معلّم كمنشور (يدويًا)",
    s_scheduled: "مجدول",
    s_went_live: "تم نشره (مجدول)",
    s_public: "عام",
    s_unlisted: "غير مدرج",
    s_private: "خاص (بدون موعد)",
    planBtn: "خطة النشر التلقائي",
    planTitle: "الخطة (لم يُرفع شيء بعد)",
    planEmpty: "لا يوجد كليب مؤهل. تظهر هنا الكليبات الجاهزة وغير المرفوعة وغير المستثناة.",
    planCap: "عرض أول {n} من {t} كليب مؤهل (حصة رفع YouTube).",
    planNeedYt: "YouTube غير مفعّل أو لم يتم تسجيل الدخول. فعّل CLIPPING_AI_YOUTUBE_ENABLED=true وشغّل `uv run clipping-ai youtube auth`.",
    confirmRun: "تأكيد: ارفع كخاص وجدول",
    planSettings: "الموعد اليومي",
    hour: "الساعة",
    minute: "الدقيقة",
    perDay: "في اليوم",
    saveSettings: "حفظ الموعد",
    runDone: "انتهى النشر التلقائي: تمت جدولة {ok}، ولم يتم {bad}.",
    r_scheduled: "تمت الجدولة",
    r_failed: "فشل",
    r_skipped: "تم التخطي",
    analytics: "تحليلات YouTube",
    analyticsHint: "أرقام للقراءة فقط لآخر 28 يومًا. الاقتراحات أدناه قواعد وليست ذكاءً اصطناعيًا، ولا يتغير شيء تلقائيًا.",
    refreshAnalytics: "تحديث التحليلات",
    analyticsOff: "التحليلات غير مربوطة. شغّل `uv run clipping-ai pipeline auth analytics` على هذا الجهاز.",
    analyticsEmpty: "لا توجد فيديوهات مرفوعة بعد.",
    colViews: "المشاهدات",
    colLikes: "الإعجابات",
    colRetention: "متوسط المشاهدة",
    colVerdict: "التقييم",
    median: "وسيط مشاهدات الفيديوهات المقيّمة: {m}",
    v_strong: "قوي",
    v_weak: "ضعيف",
    v_ok: "جيد",
    v_too_new: "جديد جدًا",
    v_not_live: "لم يُنشر بعد",
    v_judge: "لا بيانات",
    refreshed: "تم تحديث التحليلات.",
    "dec.need_more_data": "لا توجد بيانات كافية: {n} فيديو فقط مناسب للتقييم. نحتاج 3 على الأقل.",
    "dec.make_more": "{n} كليب أداؤه أفضل بكثير من الباقي. اصنع المزيد من هذا المحتوى.",
    "dec.change_content": "{n} كليب أداؤه أسوأ بكثير. راجع العنوان أو البداية أو الموضوع قبل نشر محتوى مشابه.",
    "dec.improve_hook": "{n} كليب يفقد المشاهدين مبكرًا (أقل من 35% مشاهدة). جرّب بداية أقوى.",
    "dec.all_weak": "كل الكليبات المقيّمة ضعيفة. فكّر في تغيير نوع المحتوى.",
    "dec.keep_going": "لا يوجد فائز أو خاسر واضح. استمر على الخطة الحالية.",
    "dec.no_numbers_yet": "{n} كليب منشور بلا أرقام بعد. بيانات YouTube قد تتأخر بضعة أيام.",
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
    ${ui.renderNotice(ui.esc(tr(`access_${source.access}`)), source.access === "login" ? "info" : "warning")}
    <div class="pl-check"><input type="checkbox" id="pl-allow-public" ${source.allow_public ? "checked" : ""} /><span>${ui.esc(tr("allowPublic"))}</span></div>
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
    <span class="pl-status is-${ui.attr(item.status)}">${ui.esc(tr(`st_${item.status}`))}${item.reused_job ? ` · ${ui.esc(tr("reused"))}` : ""}</span>
    <div class="pl-actions">${buttons}</div>
  </div>`;
}

function queueHtml(items) {
  if (!items.length) return `<p class="field-hint">${ui.esc(tr("empty"))}</p>`;
  return items.map(itemHtml).join("");
}


function fmtWhen(value) {
  return value ? ui.formatDateTimeLabel(value) : "";
}

function clipsHtml(clips) {
  if (!clips.length) return `<p class="field-hint">${ui.esc(tr("noClips"))}</p>`;
  const head = `<div class="pl-row pl-head"><strong>${ui.esc(tr("colClip"))}</strong><strong>${ui.esc(tr("colState"))}</strong><strong>${ui.esc(tr("colAuto"))}</strong></div>`;
  return (
    head +
    clips
      .map((clip) => {
        const when = clip.publish_at ? ` ${ui.esc(fmtWhen(clip.publish_at))}` : "";
        const link = clip.studio_url
          ? ` <a href="${ui.attr(clip.studio_url)}" target="_blank" rel="noopener noreferrer">Studio</a>`
          : "";
        const open = `<a href="#/clip/${ui.attr(clip.clip_id)}">${ui.esc(clip.title)}</a>`;
        const box =
          clip.state === "not_uploaded"
            ? `<input type="checkbox" data-pl-exclude="${ui.attr(clip.clip_id)}" ${clip.excluded ? "checked" : ""} aria-label="${ui.attr(tr("colAuto"))}" />`
            : "";
        return `<div class="pl-row"><div class="pl-link">${open}</div><span class="pl-status is-state-${ui.attr(clip.state)}">${ui.esc(tr(`s_${clip.state}`))}${when}${link}</span><div class="pl-actions">${box}</div></div>`;
      })
      .join("")
  );
}

function planHtml(plan) {
  const cfg = plan.settings;
  const settings = `<div class="pl-slot"><strong>${ui.esc(tr("planSettings"))}</strong>
    <label>${ui.esc(tr("hour"))} <input type="number" id="pl-hour" min="0" max="23" value="${cfg.publish_hour}" /></label>
    <label>${ui.esc(tr("minute"))} <input type="number" id="pl-minute" min="0" max="59" value="${cfg.publish_minute}" /></label>
    <label>${ui.esc(tr("perDay"))} <input type="number" id="pl-perday" min="1" max="5" value="${cfg.per_day}" /></label>
    <button class="button button-ghost button-small" type="button" id="pl-save-settings">${ui.esc(tr("saveSettings"))}</button></div>`;
  const buttons = `<div class="form-actions"><button class="button button-secondary" type="button" id="pl-plan">${ui.esc(tr("planBtn"))}</button></div>`;
  return settings + buttons + `<div id="pl-plan-body"></div>`;
}

function planBodyHtml(plan) {
  if (!plan.items.length) return `<p class="field-hint">${ui.esc(tr("planEmpty"))}</p>`;
  const rows = plan.items
    .map(
      (item) =>
        `<div class="pl-row"><div class="pl-link">${ui.esc(item.title)}</div><span class="pl-status">${ui.esc(fmtWhen(item.publish_at))}</span><span></span></div>`,
    )
    .join("");
  const cap = plan.capped ? `<p class="field-hint">${ui.esc(tr("planCap", { n: plan.items.length, t: plan.eligible_total }))}</p>` : "";
  const ready = plan.youtube_enabled && plan.youtube_authorised;
  const warn = ready ? "" : ui.renderNotice(ui.esc(tr("planNeedYt")), "warning");
  const ids = plan.items.map((i) => i.clip_id).join(",");
  return `<h4 class="pl-sub">${ui.esc(tr("planTitle"))}</h4>${rows}${cap}${warn}
    <div class="form-actions"><button class="button button-primary" type="button" id="pl-run" data-ids="${ui.attr(ids)}" ${ready ? "" : "disabled"}>${ui.esc(tr("confirmRun"))}</button></div>`;
}

function analyticsHtml(data) {
  const off = data.authorised ? "" : ui.renderNotice(ui.esc(tr("analyticsOff")), "warning");
  const button = `<div class="form-actions"><button class="button button-secondary" type="button" id="pl-analytics" ${data.authorised ? "" : "disabled"}>${ui.icon("refresh")}<span>${ui.esc(tr("refreshAnalytics"))}</span></button></div>`;
  if (!data.videos.length) return off + button + `<p class="field-hint">${ui.esc(tr("analyticsEmpty"))}</p>`;
  const decisions = data.decisions
    .map((d) => ui.renderNotice(ui.esc(tr(`dec.${d.kind}`, { n: d.count })), d.kind === "change_content" || d.kind === "improve_hook" || d.kind === "all_weak" ? "warning" : "info"))
    .join("");
  const median = data.median_views === null ? "" : `<p class="field-hint">${ui.esc(tr("median", { m: Math.round(data.median_views) }))}</p>`;
  const head = `<div class="pl-row pl-head"><strong>${ui.esc(tr("colClip"))}</strong><strong>${ui.esc(tr("colViews"))} / ${ui.esc(tr("colLikes"))} / ${ui.esc(tr("colRetention"))}</strong><strong>${ui.esc(tr("colVerdict"))}</strong></div>`;
  const rows = data.videos
    .map((v) => {
      const retention = v.retention === null || v.retention === undefined ? "–" : `${Math.round(v.retention * 100)}%`;
      const label = v.verdict === "judge" ? "v_judge" : `v_${v.verdict}`;
      return `<div class="pl-row"><div class="pl-link"><a href="#/clip/${ui.attr(v.clip_id)}">${ui.esc(v.title)}</a></div><span>${v.views} / ${v.likes} / ${retention}</span><span class="pl-status is-verdict-${ui.attr(v.verdict)}">${ui.esc(tr(label))}</span></div>`;
    })
    .join("");
  return off + button + decisions + median + head + rows;
}

export async function pipelineView(token, isCurrent = () => true) {
  const [source, items, review, clipRows, plan, analytics] = await Promise.all([
    call("/api/pipeline/source"),
    call("/api/pipeline/items"),
    call("/api/pipeline/review"),
    call("/api/pipeline/clips"),
    call("/api/pipeline/autopublish/plan"),
    call("/api/pipeline/analytics"),
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
    <section class="form-section" id="pl-clips">
      <div class="form-section-header"><div><h3>${ui.esc(tr("clips"))}</h3><p>${ui.esc(tr("clipsHint"))}</p></div></div>
      <div id="pl-plan-wrap">${planHtml(plan)}</div>
      <div id="pl-clips-body" class="pl-queue">${clipsHtml(clipRows.clips)}</div>
    </section>
    <section class="form-section" id="pl-analytics-section">
      <div class="form-section-header"><div><h3>${ui.esc(tr("analytics"))}</h3><p>${ui.esc(tr("analyticsHint"))}</p></div></div>
      <div id="pl-analytics-body" class="pl-queue">${analyticsHtml(analytics)}</div>
    </section>
  </div>`;

  async function refresh(root) {
    const [s, i, r, c, a] = await Promise.all([
      call("/api/pipeline/source"),
      call("/api/pipeline/items"),
      call("/api/pipeline/review"),
      call("/api/pipeline/clips"),
      call("/api/pipeline/analytics"),
    ]);
    if (!isCurrent()) return;
    root.querySelector("#pl-clips-body").innerHTML = clipsHtml(c.clips);
    root.querySelector("#pl-analytics-body").innerHTML = analyticsHtml(a);
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
    root.addEventListener("change", (event) => {
      const box = event.target.closest("input[data-pl-exclude]");
      if (!box) return;
      call(`/api/pipeline/clips/${box.dataset.plExclude}/exclude`, {
        method: "PUT",
        body: { excluded: box.checked },
      }).catch((error) => ui.toast(error.message, "error"));
    });
    root.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      if (button.id === "pl-save") {
        const value = root.querySelector("#pl-sheet-url").value.trim();
        run(button, async () => {
          const allow = root.querySelector("#pl-allow-public")?.checked === true;
          await call("/api/pipeline/source", {
            method: "PUT",
            body: { sheet_url: value, allow_public: allow },
          });
          ui.toast(tr("saved"), "success");
        });
      } else if (button.id === "pl-sync") {
        run(button, async () => {
          const result = await call("/api/pipeline/sync", { method: "POST" });
          ui.toast(
            tr("synced", { n: result.added }) + (result.duplicates ? tr("dupSynced", { d: result.duplicates }) : ""),
            "success",
          );
        });
      } else if (button.id === "pl-save-settings") {
        run(button, async () => {
          await call("/api/pipeline/settings", {
            method: "PUT",
            body: {
              publish_hour: Number(root.querySelector("#pl-hour").value),
              publish_minute: Number(root.querySelector("#pl-minute").value),
              per_day: Number(root.querySelector("#pl-perday").value),
            },
          });
          ui.toast(tr("saved"), "success");
        });
      } else if (button.id === "pl-plan") {
        run(button, async () => {
          const plan = await call("/api/pipeline/autopublish/plan");
          root.querySelector("#pl-plan-body").innerHTML = planBodyHtml(plan);
        });
      } else if (button.id === "pl-run") {
        const ids = button.dataset.ids.split(",").filter(Boolean);
        run(button, async () => {
          const out = await call("/api/pipeline/autopublish/run", {
            method: "POST",
            body: { clip_ids: ids, confirm: true },
          });
          const ok = out.results.filter((r) => r.status === "scheduled").length;
          const failed = out.results.filter((r) => r.status === "failed");
          ui.toast(tr("runDone", { ok, bad: out.results.length - ok }), failed.length ? "error" : "success");
          if (failed.length) ui.toast(failed[0].error, "error");
          root.querySelector("#pl-plan-body").innerHTML = "";
        });
      } else if (button.id === "pl-analytics") {
        run(button, async () => {
          await call("/api/pipeline/analytics/refresh", { method: "POST" });
          ui.toast(tr("refreshed"), "success");
        });
      } else if (button.dataset.pl) {
        const id = button.dataset.id;
        run(button, async () => {
          const result = await call(`/api/pipeline/items/${id}/${button.dataset.pl}`, {
            method: "POST",
          });
          if (button.dataset.pl === "send") {
            ui.toast(
              result.duplicate ? tr("sentExisting", { c: result.existing_clips }) : tr("sentOk"),
              "success",
            );
          }
        });
      }
    });
  }

  return { title: tr("title"), html, mount };
}
