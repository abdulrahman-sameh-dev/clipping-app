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
    week: "Next 14 days: scheduled releases",
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
    access_locked: "Locked: no sign-in yet, so nothing is read. Connect Google Sheets under Connections.",
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
    planNeedYt: "YouTube is not enabled or upload is not connected for this channel. Set CLIPPING_AI_YOUTUBE_ENABLED=true and connect YouTube upload under Connections.",
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
    analyticsOff: "Analytics is not connected for this channel. Connect it under Connections.",
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
    "tip.nothing_scheduled": "Nothing is scheduled for the next 14 days.",
    "tip.empty_days": "{n} of the next 14 days have no scheduled release.",
    channel: "Channel",
    channelHint: "Each channel has its own category, daily slot, sheet link, analytics rules and YouTube sign-ins.",
    channelName: "Name",
    channelCategory: "Category",
    addChannel: "Add channel",
    saveChannel: "Save channel settings",
    channelSaved: "Channel saved.",
    channelAdded: "Channel added.",
    strongRatio: "Strong at (x median)",
    weakRatio: "Weak at (x median)",
    "cat.general": "General",
    "cat.gaming": "Gaming",
    "cat.religious": "Religious",
    "cat.educational": "Educational",
    "cat.entertainment": "Entertainment",
    "cat.news": "News",
    "cat.other": "Other",
    connections: "Connections",
    connectionsHint: "Sign in with Google here, no terminal needed. Every sign-in uses one narrow permission and can be removed at any time. Tokens stay on this computer.",
    connNeedClient: "Google app keys are missing. Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env and restart (see YOUTUBE_SETUP.md).",
    connRedirect: "If Google shows redirect_uri_mismatch, use a Desktop-app OAuth client, or add this exact redirect URI to your OAuth client: {uri}",
    connConnected: "Connected",
    connNot: "Not connected",
    connReconnect: "Reconnect needed (new permission)",
    connect: "Connect",
    reconnect: "Reconnect",
    disconnect: "Disconnect",
    disconnected: "Disconnected on this computer. You can also remove the app in your Google account.",
    "conn.sheets": "Google Sheets (read-only), shared by all channels",
    "conn.sheetsHint": "Reads your private sheet. Cannot change it.",
    "conn.analytics": "YouTube Analytics and schedule (read-only)",
    "conn.analyticsHint": "Reads numbers and the videos already scheduled on this channel. Cannot upload or change anything.",
    "conn.upload": "YouTube upload and scheduling",
    "conn.uploadHint": "The only permission that can publish. Used only when you confirm a plan or upload a clip.",
    connectedOk: "Connected: {kind}.",
    connectDenied: "Sign-in was cancelled. Nothing was saved.",
    connectFailed: "Sign-in failed. Nothing was saved. Try again.",
    pickChannelHint: "When Google asks, choose the account and the YouTube channel that belong to this channel.",
    schedule: "Scheduled on the channel",
    scheduleHint: "Videos already scheduled on YouTube (also the ones you scheduled yourself). The plan and the daily review use these, so they never double-book a day.",
    scheduleRefresh: "Read schedule now",
    scheduleYoutube: "YouTube channel: {t}",
    scheduleNone: "Nothing scheduled on this channel in the future.",
    scheduleOff: "Not connected: the plan only knows the clips uploaded by this tool. Connect YouTube Analytics and schedule above.",
    scheduleReconnect: "Your Analytics sign-in was made before schedule reading existed. Reconnect it above.",
    scheduleError: "Last read failed: {e}",
    scheduleRead: "Read at {t}.",
    scheduleReadOk: "Schedule updated.",
    planSees: "The plan sees {n} video(s) already scheduled on the channel.",
    planBlind: "The channel's schedule is unknown, so the plan may overlap videos you scheduled yourself.",
    allowUnknown: "Upload anyway without the channel schedule",
    "tip.schedule_unknown": "The channel's own schedule is not known, so the free days below only count this tool's uploads. Connect YouTube Analytics and schedule.",
    "conn.manage": "Edit scheduled times on YouTube",
    "conn.manageHint": "Optional. Only needed to change the release time of videos already scheduled (day page and reschedule). Wider permission: it can edit your channel's videos, but this app only changes the time of videos that are still waiting to go live, and only after you confirm.",
    dayOpen: "Open this day",
    dayBack: "Back to Pipeline",
    dayPrev: "Previous day",
    dayNext: "Next day",
    dayTitle: "Schedule for {d}",
    dayPast: "This day is in the past. It is shown for reference only.",
    dayNone: "Nothing is scheduled on this day.",
    dayVideos: "Scheduled videos",
    dayMove: "Move",
    dayMoveTitle: "Change release time",
    dayMoveAsk: "Move \"{t}\" from {a} to {b}? Nothing else about the video changes.",
    dayMoved: "Release time changed.",
    dayNeedManage: "To change times here, connect \"Edit scheduled times on YouTube\" in Connections on the Pipeline page. Until then this page is read-only.",
    dayAdd: "Add a ready clip to this day",
    dayAddHint: "Uploads the clip privately and YouTube publishes it at the time you pick.",
    dayAddNone: "No ready clips are waiting.",
    dayAddBtn: "Upload and schedule here",
    dayAddAsk: "Upload \"{t}\" as private and schedule it for {b}?",
    dayAdded: "Uploaded privately and scheduled.",
    dayNeedUpload: "Uploading needs the YouTube upload connection (Connections on the Pipeline page).",
    dayPickClip: "Clip",
    dayTime: "Time",
    dayOpenStudio: "Studio",
    dayTooSoon: "Pick a time at least 30 minutes from now.",
    resched: "Reschedule by analytics",
    reschedHint: "Looks at which publish hours got more views per day and proposes new times for videos that are already scheduled. It shows old and new times first. Nothing changes on YouTube until you confirm.",
    reschedBuild: "Build reschedule plan",
    reschedNeedManage: "Applying needs \"Edit scheduled times on YouTube\" (Connections). You can still see the plan.",
    reschedNumbers: "Based on {n} past video(s). Numbers read {t}. Refresh the analytics above first if they are old.",
    reschedNoNumbers: "No analytics numbers have been read yet. Refresh the analytics above first.",
    "reschedWhy.not_enough_videos": "Not enough data to judge posting times: only {n} past video(s) have numbers, and at least {m} are needed. Nothing was changed.",
    "reschedWhy.not_enough_hours": "Not enough variety: the videos were all posted at nearly the same hour, so there is nothing to compare. Nothing was changed.",
    "reschedWhy.no_clear_difference": "The publish hours performed about the same (best hour is less than {x} times the others). Changing times would be a guess, so nothing was changed.",
    reschedBest: "Best hours so far (views per day, median): {h}.",
    reschedNothing: "All scheduled videos already sit at the best hours.",
    reschedNoUpcoming: "Nothing is scheduled to change.",
    reschedSkipped: "{n} video(s) due very soon were left alone.",
    reschedCapped: "Only the first {n} changes are shown. Run it again after applying.",
    reschedOld: "Now",
    reschedNew: "New",
    reschedApply: "Confirm: change these times on YouTube",
    reschedDone: "Times changed: {ok}. Not changed: {bad}.",
  },
  ar: {
    nav: "خط الإنتاج",
    title: "خط إنتاج المحتوى",
    description: "روابط من Google Sheet محفوظة على جهازك، ومراجعة يومية لما يجب فعله بعد ذلك.",
    review: "المراجعة اليومية",
    reviewHint: "ما يحتاج قرارًا اليوم. هذه اقتراحات فقط ولا يعمل شيء تلقائيًا.",
    week: "الأيام الأربعة عشر القادمة: مواعيد النشر",
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
    access_locked: "مقفل: لا يوجد تسجيل دخول بعد، فلا يُقرأ شيء. اربط Google Sheets من قسم الربط.",
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
    planNeedYt: "YouTube غير مفعّل أو رفع YouTube غير مربوط لهذه القناة. فعّل CLIPPING_AI_YOUTUBE_ENABLED=true واربط رفع YouTube من قسم الربط.",
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
    analyticsOff: "التحليلات غير مربوطة لهذه القناة. اربطها من قسم الربط.",
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
    "tip.nothing_scheduled": "لا يوجد شيء مجدول خلال الأيام الأربعة عشر القادمة.",
    "tip.empty_days": "{n} من الأيام الأربعة عشر القادمة بلا موعد نشر.",
    channel: "القناة",
    channelHint: "لكل قناة تصنيفها وموعدها اليومي ورابط شيتها وقواعد تحليلها وتسجيلات دخول YouTube الخاصة بها.",
    channelName: "الاسم",
    channelCategory: "التصنيف",
    addChannel: "إضافة قناة",
    saveChannel: "حفظ إعدادات القناة",
    channelSaved: "تم حفظ القناة.",
    channelAdded: "تمت إضافة القناة.",
    strongRatio: "قوي عند (× الوسيط)",
    weakRatio: "ضعيف عند (× الوسيط)",
    "cat.general": "عام",
    "cat.gaming": "ألعاب",
    "cat.religious": "ديني",
    "cat.educational": "تعليمي",
    "cat.entertainment": "ترفيه",
    "cat.news": "أخبار",
    "cat.other": "أخرى",
    connections: "الربط",
    connectionsHint: "سجّل الدخول بحساب Google من هنا بدون terminal. كل تسجيل يستخدم صلاحية واحدة ضيقة ويمكن إزالته في أي وقت. التوكن يبقى على هذا الجهاز.",
    connNeedClient: "مفاتيح تطبيق Google ناقصة. اضبط YOUTUBE_CLIENT_ID وYOUTUBE_CLIENT_SECRET في ملف .env ثم أعد التشغيل (راجع YOUTUBE_SETUP.md).",
    connRedirect: "إذا ظهر redirect_uri_mismatch من Google فاستخدم OAuth client من نوع Desktop، أو أضف رابط إعادة التوجيه هذا بالضبط: {uri}",
    connConnected: "مربوط",
    connNot: "غير مربوط",
    connReconnect: "يلزم إعادة الربط (صلاحية جديدة)",
    connect: "ربط",
    reconnect: "إعادة الربط",
    disconnect: "فصل",
    disconnected: "تم الفصل على هذا الجهاز. يمكنك أيضًا إزالة التطبيق من حساب Google.",
    "conn.sheets": "Google Sheets (قراءة فقط)، مشترك بين كل القنوات",
    "conn.sheetsHint": "يقرأ الشيت الخاص بك ولا يستطيع تعديله.",
    "conn.analytics": "تحليلات YouTube والجدولة (قراءة فقط)",
    "conn.analyticsHint": "يقرأ الأرقام والفيديوهات المجدولة بالفعل على هذه القناة. لا يستطيع الرفع أو التعديل.",
    "conn.upload": "رفع وجدولة YouTube",
    "conn.uploadHint": "الصلاحية الوحيدة التي تستطيع النشر. تُستخدم فقط عندما تؤكد خطة أو ترفع كليبًا.",
    connectedOk: "تم الربط: {kind}.",
    connectDenied: "تم إلغاء تسجيل الدخول. لم يُحفظ شيء.",
    connectFailed: "فشل تسجيل الدخول. لم يُحفظ شيء. حاول مرة أخرى.",
    pickChannelHint: "عندما يسألك Google، اختر الحساب وقناة YouTube الخاصة بهذه القناة.",
    schedule: "المجدول على القناة",
    scheduleHint: "الفيديوهات المجدولة بالفعل على YouTube (بما فيها ما جدولته بنفسك). الخطة والمراجعة اليومية تعتمدان عليها فلا يحدث حجز مزدوج ليوم.",
    scheduleRefresh: "اقرأ الجدولة الآن",
    scheduleYoutube: "قناة YouTube: {t}",
    scheduleNone: "لا يوجد شيء مجدول في المستقبل على هذه القناة.",
    scheduleOff: "غير مربوط: الخطة لا تعرف إلا الكليبات التي رفعتها هذه الأداة. اربط تحليلات YouTube والجدولة بالأعلى.",
    scheduleReconnect: "تسجيل دخول التحليلات تم قبل وجود قراءة الجدولة. أعد ربطه بالأعلى.",
    scheduleError: "فشلت آخر قراءة: {e}",
    scheduleRead: "آخر قراءة: {t}.",
    scheduleReadOk: "تم تحديث الجدولة.",
    planSees: "الخطة ترى {n} فيديو مجدول بالفعل على القناة.",
    planBlind: "جدولة القناة غير معروفة، فقد تتداخل الخطة مع فيديوهات جدولتها بنفسك.",
    allowUnknown: "ارفع رغم ذلك بدون جدولة القناة",
    "tip.schedule_unknown": "جدولة القناة نفسها غير معروفة، فالأيام الفاضية بالأسفل تحسب رفعات هذه الأداة فقط. اربط تحليلات YouTube والجدولة.",
    "conn.manage": "تعديل مواعيد النشر على YouTube",
    "conn.manageHint": "اختياري. لازم فقط لتغيير موعد نشر فيديوهات مجدولة بالفعل (صفحة اليوم وإعادة الجدولة). صلاحية أوسع: تسمح بتعديل فيديوهات قناتك، لكن التطبيق يغيّر فقط موعد الفيديوهات التي لم تُنشر بعد، وبعد تأكيدك فقط.",
    dayOpen: "افتح هذا اليوم",
    dayBack: "الرجوع لخط الإنتاج",
    dayPrev: "اليوم السابق",
    dayNext: "اليوم التالي",
    dayTitle: "جدول يوم {d}",
    dayPast: "هذا اليوم في الماضي. معروض للاطلاع فقط.",
    dayNone: "لا يوجد شيء مجدول في هذا اليوم.",
    dayVideos: "الفيديوهات المجدولة",
    dayMove: "نقل",
    dayMoveTitle: "تغيير موعد النشر",
    dayMoveAsk: "نقل \"{t}\" من {a} إلى {b}؟ لا يتغير أي شيء آخر في الفيديو.",
    dayMoved: "تم تغيير موعد النشر.",
    dayNeedManage: "لتغيير المواعيد من هنا اربط \"تعديل مواعيد النشر على YouTube\" من قسم الربط في صفحة خط الإنتاج. حتى ذلك الحين الصفحة للقراءة فقط.",
    dayAdd: "أضف كليبًا جاهزًا لهذا اليوم",
    dayAddHint: "يرفع الكليب كخاص ويقوم YouTube بنشره في الموعد الذي تختاره.",
    dayAddNone: "لا توجد كليبات جاهزة.",
    dayAddBtn: "ارفع وجدول هنا",
    dayAddAsk: "رفع \"{t}\" كخاص وجدولته على {b}؟",
    dayAdded: "تم الرفع كخاص والجدولة.",
    dayNeedUpload: "الرفع يحتاج ربط رفع YouTube (قسم الربط في صفحة خط الإنتاج).",
    dayPickClip: "الكليب",
    dayTime: "الوقت",
    dayOpenStudio: "Studio",
    dayTooSoon: "اختر وقتًا بعد 30 دقيقة على الأقل من الآن.",
    resched: "إعادة الجدولة حسب التحليلات",
    reschedHint: "ينظر في أي ساعات نشر حققت مشاهدات أكثر في اليوم، ويقترح مواعيد جديدة للفيديوهات المجدولة بالفعل. يعرض لك القديم والجديد أولًا. لا يتغير شيء على YouTube إلا بعد تأكيدك.",
    reschedBuild: "ابنِ خطة إعادة الجدولة",
    reschedNeedManage: "التطبيق يحتاج \"تعديل مواعيد النشر على YouTube\" (قسم الربط). تقدر تشوف الخطة بدونه.",
    reschedNumbers: "مبنية على {n} فيديو سابق. آخر قراءة للأرقام {t}. حدّث التحليلات بالأعلى أولًا لو قديمة.",
    reschedNoNumbers: "لم تُقرأ أي أرقام تحليلات بعد. حدّث التحليلات بالأعلى أولًا.",
    "reschedWhy.not_enough_videos": "البيانات لا تكفي للحكم على أوقات النشر: عدد الفيديوهات السابقة التي لها أرقام {n} فقط، والمطلوب {m} على الأقل. لم يتغير شيء.",
    "reschedWhy.not_enough_hours": "لا يوجد تنوع كافٍ: كل الفيديوهات نُشرت تقريبًا في نفس الساعة فلا يوجد ما نقارنه. لم يتغير شيء.",
    "reschedWhy.no_clear_difference": "ساعات النشر أعطت نتائج متقاربة (أفضل ساعة أقل من {x} مرة من الباقي). تغيير المواعيد سيكون تخمينًا، فلم يتغير شيء.",
    reschedBest: "أفضل الساعات حتى الآن (مشاهدات يوميًا، الوسيط): {h}.",
    reschedNothing: "كل الفيديوهات المجدولة بالفعل في أفضل الساعات.",
    reschedNoUpcoming: "لا يوجد شيء مجدول لتغييره.",
    reschedSkipped: "تُرك {n} فيديو موعده قريب جدًا.",
    reschedCapped: "تظهر أول {n} تغييرات فقط. شغّلها مرة أخرى بعد التطبيق.",
    reschedOld: "الآن",
    reschedNew: "الجديد",
    reschedApply: "تأكيد: غيّر هذه المواعيد على YouTube",
    reschedDone: "تم تغيير: {ok}. لم يتغير: {bad}.",
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

const CHANNEL_KEY = "clipping.pipeline.channel";
let channelId = "default";
try {
  channelId = window.localStorage.getItem(CHANNEL_KEY) || "default";
} catch {
  channelId = "default";
}
function inChannel(path) {
  return `${path}${path.includes("?") ? "&" : "?"}channel=${encodeURIComponent(channelId)}`;
}
function chooseChannel(id) {
  channelId = id;
  try {
    window.localStorage.setItem(CHANNEL_KEY, id);
  } catch {
    /* the choice is kept for this page only */
  }
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

function channelOptions(channels, selected) {
  return channels
    .map((c) => `<option value="${ui.attr(c.id)}" ${c.id === selected ? "selected" : ""}>${ui.esc(c.name)}</option>`)
    .join("");
}

function categoryOptions(categories, selected) {
  return categories
    .map((c) => `<option value="${ui.attr(c)}" ${c === selected ? "selected" : ""}>${ui.esc(tr(`cat.${c}`))}</option>`)
    .join("");
}

function channelHtml(data) {
  const current = data.channels.find((c) => c.id === channelId) || data.channels[0];
  return `<div class="pl-slot"><label>${ui.esc(tr("channel"))}
      <select id="pl-channel">${channelOptions(data.channels, current.id)}</select></label></div>
    <div class="pl-slot">
      <label>${ui.esc(tr("channelName"))} <input type="text" id="pl-ch-name" maxlength="60" value="${ui.attr(current.name)}" /></label>
      <label>${ui.esc(tr("channelCategory"))} <select id="pl-ch-category">${categoryOptions(data.categories, current.category)}</select></label>
      <label>${ui.esc(tr("strongRatio"))} <input type="number" id="pl-ch-strong" step="0.1" min="1.1" max="10" value="${current.strong_ratio}" /></label>
      <label>${ui.esc(tr("weakRatio"))} <input type="number" id="pl-ch-weak" step="0.05" min="0.05" max="0.9" value="${current.weak_ratio}" /></label>
      <button class="button button-secondary button-small" type="button" id="pl-ch-save">${ui.esc(tr("saveChannel"))}</button>
    </div>
    <div class="pl-slot">
      <label>${ui.esc(tr("channelName"))} <input type="text" id="pl-new-name" maxlength="60" /></label>
      <label>${ui.esc(tr("channelCategory"))} <select id="pl-new-category">${categoryOptions(data.categories, "general")}</select></label>
      <button class="button button-ghost button-small" type="button" id="pl-ch-add">${ui.esc(tr("addChannel"))}</button>
    </div>`;
}

function connectionRow(kind, state) {
  let label = tr("connNot");
  if (state.connected) label = tr("connConnected");
  if (kind === "analytics" && state.connected && !state.schedule_ready) label = tr("connReconnect");
  const warn = kind === "analytics" && state.connected && !state.schedule_ready;
  const action = state.connected
    ? `<button class="button button-secondary button-small" type="button" data-connect="${kind}">${ui.esc(tr("reconnect"))}</button>
       <button class="button button-ghost button-small" type="button" data-disconnect="${kind}">${ui.esc(tr("disconnect"))}</button>`
    : `<button class="button button-primary button-small" type="button" data-connect="${kind}">${ui.esc(tr("connect"))}</button>`;
  return `<div class="pl-row"><div class="pl-link"><strong>${ui.esc(tr(`conn.${kind}`))}</strong><br /><small class="muted">${ui.esc(tr(`conn.${kind}Hint`))}</small></div>
    <span class="pl-status ${state.connected && !warn ? "is-sent" : "is-new"}">${ui.esc(label)}</span>
    <div class="pl-actions">${action}</div></div>`;
}

function connectionsHtml(status) {
  const client = status.client_configured ? "" : ui.renderNotice(ui.esc(tr("connNeedClient")), "warning");
  const hint = `<p class="field-hint">${ui.esc(tr("pickChannelHint"))}</p><p class="field-hint" dir="ltr">${ui.esc(tr("connRedirect", { uri: status.redirect_uri }))}</p>`;
  const rows = ["sheets", "analytics", "upload", "manage"].map((kind) => connectionRow(kind, status.kinds[kind])).join("");
  return client + hint + rows;
}

function scheduleHtml(data) {
  let body = "";
  if (data.reconnect_needed) body += ui.renderNotice(ui.esc(tr("scheduleReconnect")), "warning");
  else if (!data.can_read) body += ui.renderNotice(ui.esc(tr("scheduleOff")), "warning");
  if (data.error) body += ui.renderNotice(ui.esc(tr("scheduleError", { e: data.error })), "error");
  if (data.can_read) {
    const info = [data.youtube_channel_title ? tr("scheduleYoutube", { t: data.youtube_channel_title }) : "", data.fetched_at ? tr("scheduleRead", { t: ui.formatDateTimeLabel(data.fetched_at) }) : ""]
      .filter(Boolean)
      .join(" ");
    if (info) body += `<p class="field-hint">${ui.esc(info)}</p>`;
    body += `<div class="form-actions"><button class="button button-secondary" type="button" id="pl-schedule-refresh">${ui.icon("refresh")}<span>${ui.esc(tr("scheduleRefresh"))}</span></button></div>`;
    body += data.videos.length
      ? data.videos
          .map((v) => `<div class="pl-row"><div class="pl-link">${ui.esc(v.title || v.video_id)}</div><span class="pl-status is-state-scheduled">${ui.esc(fmtWhen(v.publish_at))}</span><span></span></div>`)
          .join("")
      : `<p class="field-hint">${ui.esc(tr("scheduleNone"))}</p>`;
  }
  return body;
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
      return `<a class="pl-day ${day.count ? "has-items" : ""}" href="#/pipeline/day/${ui.attr(day.date)}" title="${ui.attr(tr("dayOpen"))}"><span>${ui.esc(label)}</span><strong>${day.count}</strong></a>`;
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
  const seen = plan.schedule_known
    ? `<p class="field-hint">${ui.esc(tr("planSees", { n: plan.scheduled_on_channel }))}</p>`
    : ui.renderNotice(ui.esc(tr("planBlind")), "warning") +
      `<div class="pl-check"><input type="checkbox" id="pl-allow-unknown" /><span>${ui.esc(tr("allowUnknown"))}</span></div>`;
  const ids = plan.items.map((i) => i.clip_id).join(",");
  return `<h4 class="pl-sub">${ui.esc(tr("planTitle"))}</h4>${rows}${cap}${seen}${warn}
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

function hourLabel(hour) {
  return `${String(hour).padStart(2, "0")}:00`;
}

function reschedPlanHtml(plan) {
  let out = "";
  if (!plan.analytics_authorised && !plan.numbers_read_at) {
    out += ui.renderNotice(ui.esc(tr("reschedNoNumbers")), "warning");
  }
  if (plan.numbers_read_at) {
    out += `<p class="field-hint">${ui.esc(tr("reschedNumbers", { n: plan.samples, t: ui.formatDateTimeLabel(plan.numbers_read_at) }))}</p>`;
  }
  if (plan.confidence !== "ok") {
    const text = tr(`reschedWhy.${plan.reason}`, {
      n: plan.samples,
      m: plan.rules.min_videos,
      x: plan.rules.min_lift,
    });
    return out + ui.renderNotice(ui.esc(text), "warning");
  }
  if (plan.hours.length) {
    const hours = plan.hours.slice(0, 4).map((h) => `${hourLabel(h.hour)} (${h.median_rate})`).join(", ");
    out += `<p class="field-hint">${ui.esc(tr("reschedBest", { h: hours }))}</p>`;
  }
  if (!plan.upcoming) return out + `<p class="field-hint">${ui.esc(tr("reschedNoUpcoming"))}</p>`;
  if (plan.skipped_too_soon) out += `<p class="field-hint">${ui.esc(tr("reschedSkipped", { n: plan.skipped_too_soon }))}</p>`;
  if (!plan.items.length) return out + `<p class="field-hint">${ui.esc(tr("reschedNothing"))}</p>`;
  const head = `<div class="pl-row pl-head"><strong>${ui.esc(tr("colClip"))}</strong><strong>${ui.esc(tr("reschedOld"))}</strong><strong>${ui.esc(tr("reschedNew"))}</strong></div>`;
  const rows = plan.items
    .map(
      (i) =>
        `<div class="pl-row"><div class="pl-link">${ui.esc(i.title)}</div><span>${ui.esc(fmtWhen(i.old_publish_at))}</span><span class="pl-status is-state-scheduled">${ui.esc(fmtWhen(i.new_publish_at))}</span></div>`,
    )
    .join("");
  const cap = plan.capped ? `<p class="field-hint">${ui.esc(tr("reschedCapped", { n: plan.items.length }))}</p>` : "";
  const need = plan.manage_connected ? "" : ui.renderNotice(ui.esc(tr("reschedNeedManage")), "warning");
  const data = ui.attr(JSON.stringify(plan.items.map((i) => ({ video_id: i.video_id, new_publish_at: i.new_publish_at }))));
  return `${out}${head}${rows}${cap}${need}<div class="form-actions"><button class="button button-primary" type="button" id="pl-resched-run" data-items="${data}" ${plan.manage_connected ? "" : "disabled"}>${ui.esc(tr("reschedApply"))}</button></div>`;
}

function reschedSectionHtml() {
  return `<section class="form-section" id="pl-resched-section">
      <div class="form-section-header"><div><h3>${ui.esc(tr("resched"))}</h3><p>${ui.esc(tr("reschedHint"))}</p></div></div>
      <div class="form-actions"><button class="button button-secondary" type="button" id="pl-resched-build" data-keep="1">${ui.esc(tr("reschedBuild"))}</button></div>
      <div id="pl-resched-body" class="pl-queue"></div>
    </section>`;
}

function toLocalInput(iso) {
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function shiftDay(day, delta) {
  const d = new Date(`${day}T12:00:00`);
  d.setDate(d.getDate() + delta);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function dayBodyHtml(data) {
  const label = new Date(`${data.date}T12:00:00`).toLocaleDateString(getLocale(), {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  });
  const nav = `<div class="form-actions pl-daynav"><a class="button button-secondary button-small" href="#/pipeline">${ui.esc(tr("dayBack"))}</a>
    <a class="button button-ghost button-small" href="#/pipeline/day/${ui.attr(shiftDay(data.date, -1))}">${ui.esc(tr("dayPrev"))}</a>
    <a class="button button-ghost button-small" href="#/pipeline/day/${ui.attr(shiftDay(data.date, 1))}">${ui.esc(tr("dayNext"))}</a></div>`;
  const past = data.is_past ? ui.renderNotice(ui.esc(tr("dayPast")), "info") : "";
  const needManage = !data.manage_connected && data.videos.length && !data.is_past
    ? ui.renderNotice(ui.esc(tr("dayNeedManage")), "warning")
    : "";
  const editable = data.manage_connected && !data.is_past;
  const videos = data.videos.length
    ? data.videos
        .map((v) => {
          const studio = v.studio_url ? ` <a href="${ui.attr(v.studio_url)}" target="_blank" rel="noopener noreferrer">${ui.esc(tr("dayOpenStudio"))}</a>` : "";
          const edit = editable
            ? `<div class="pl-actions"><input type="datetime-local" data-day-time="${ui.attr(v.video_id)}" value="${ui.attr(toLocalInput(v.publish_at))}" />
               <button class="button button-primary button-small" type="button" data-day-move="${ui.attr(v.video_id)}" data-title="${ui.attr(v.title)}" data-old="${ui.attr(v.publish_at)}">${ui.esc(tr("dayMove"))}</button></div>`
            : `<span></span>`;
          return `<div class="pl-row"><div class="pl-link">${ui.esc(v.title)}${studio}</div><span class="pl-status is-state-scheduled">${ui.esc(fmtWhen(v.publish_at))}</span>${edit}</div>`;
        })
        .join("")
    : `<p class="field-hint">${ui.esc(tr("dayNone"))}</p>`;
  let add = "";
  if (!data.is_past) {
    const base = `${data.date}T${data.default_time}`;
    if (!data.ready_clips.length) add = `<p class="field-hint">${ui.esc(tr("dayAddNone"))}</p>`;
    else {
      const options = data.ready_clips.map((c) => `<option value="${ui.attr(c.clip_id)}">${ui.esc(c.title)}</option>`).join("");
      const ready = data.youtube_enabled && data.upload_authorised;
      const warn = ready ? "" : ui.renderNotice(ui.esc(tr("dayNeedUpload")), "warning");
      const unknown = data.schedule_known
        ? ""
        : ui.renderNotice(ui.esc(tr("planBlind")), "warning") +
          `<div class="pl-check"><input type="checkbox" id="pl-day-allow-unknown" /><span>${ui.esc(tr("allowUnknown"))}</span></div>`;
      add = `<p class="field-hint">${ui.esc(tr("dayAddHint"))}</p>${warn}${unknown}
        <div class="pl-slot"><label>${ui.esc(tr("dayPickClip"))} <select id="pl-day-clip">${options}</select></label>
        <label>${ui.esc(tr("dayTime"))} <input type="datetime-local" id="pl-day-new-time" value="${ui.attr(base)}" /></label>
        <button class="button button-primary button-small" type="button" id="pl-day-add" ${ready ? "" : "disabled"}>${ui.esc(tr("dayAddBtn"))}</button></div>`;
    }
  }
  return `${nav}<h1>${ui.esc(tr("dayTitle", { d: label }))}</h1>${past}
    <section class="form-section"><div class="form-section-header"><div><h3>${ui.esc(tr("dayVideos"))}</h3></div></div>${needManage}<div class="pl-queue">${videos}</div></section>
    ${data.is_past ? "" : `<section class="form-section"><div class="form-section-header"><div><h3>${ui.esc(tr("dayAdd"))}</h3></div></div>${add}</section>`}`;
}

export async function pipelineDayView(day, token, isCurrent = () => true) {
  const channels = await call("/api/pipeline/channels");
  if (!channels.channels.some((c) => c.id === channelId)) chooseChannel(channels.channels[0].id);
  const data = await call(inChannel(`/api/pipeline/day?date=${encodeURIComponent(day)}`));
  const html = `<div class="page-enter pl-page pl-daypage" id="pl-daypage">${dayBodyHtml(data)}</div>`;

  function mount() {
    const root = document.getElementById("pl-daypage");
    if (!root) return;
    const reload = async () => {
      const fresh = await call(inChannel(`/api/pipeline/day?date=${encodeURIComponent(day)}`));
      if (isCurrent()) root.innerHTML = dayBodyHtml(fresh);
    };
    const guard = async (button, work) => {
      if (button.disabled) return;
      button.disabled = true;
      try {
        await work();
      } catch (error) {
        ui.toast(error.message, "error");
      } finally {
        try {
          await reload();
        } catch {
          button.disabled = false;
        }
      }
    };
    root.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      if (button.dataset.dayMove) {
        const id = button.dataset.dayMove;
        const input = root.querySelector(`input[data-day-time="${CSS.escape(id)}"]`);
        guard(button, async () => {
          if (!input || !input.value) return;
          const picked = new Date(input.value);
          if (picked.getTime() < Date.now() + 30 * 60 * 1000) {
            ui.toast(tr("dayTooSoon"), "error");
            return;
          }
          const ok = await ui.confirmDialog(
            tr("dayMoveAsk", { t: button.dataset.title, a: fmtWhen(button.dataset.old), b: fmtWhen(picked.toISOString()) }),
            { title: tr("dayMoveTitle"), confirmLabel: tr("dayMove") },
          );
          if (!ok) return;
          await call(inChannel(`/api/pipeline/day/video/${encodeURIComponent(id)}/time`), {
            method: "PUT",
            body: { publish_at: picked.toISOString(), confirm: true, channel_id: channelId },
          });
          ui.toast(tr("dayMoved"), "success");
        });
      } else if (button.id === "pl-day-add") {
        guard(button, async () => {
          const select = root.querySelector("#pl-day-clip");
          const time = root.querySelector("#pl-day-new-time");
          if (!select || !time || !time.value) return;
          const picked = new Date(time.value);
          if (picked.getTime() < Date.now() + 30 * 60 * 1000) {
            ui.toast(tr("dayTooSoon"), "error");
            return;
          }
          const title = select.options[select.selectedIndex].text;
          const ok = await ui.confirmDialog(tr("dayAddAsk", { t: title, b: fmtWhen(picked.toISOString()) }), {
            title: tr("dayAdd"),
            confirmLabel: tr("dayAddBtn"),
          });
          if (!ok) return;
          await call("/api/pipeline/day/add", {
            method: "POST",
            body: {
              clip_id: select.value,
              publish_at: picked.toISOString(),
              confirm: true,
              channel_id: channelId,
              allow_unknown_schedule: root.querySelector("#pl-day-allow-unknown")?.checked === true,
            },
          });
          ui.toast(tr("dayAdded"), "success");
        });
      }
    });
  }

  return { title: tr("title"), html, mount };
}

export async function pipelineView(token, isCurrent = () => true) {
  const channels = await call("/api/pipeline/channels");
  if (!channels.channels.some((c) => c.id === channelId)) chooseChannel(channels.channels[0].id);
  const [source, items, review, clipRows, plan, analytics, connStatus, sched] = await Promise.all([
    call(inChannel("/api/pipeline/source")),
    call(inChannel("/api/pipeline/items")),
    call(inChannel("/api/pipeline/review")),
    call(inChannel("/api/pipeline/clips")),
    call(inChannel("/api/pipeline/autopublish/plan")),
    call(inChannel("/api/pipeline/analytics")),
    call(inChannel("/api/google/status")),
    call(inChannel("/api/pipeline/schedule")),
  ]);
  const html = `<div class="page-enter pl-page">
    <div class="page-head"><div class="page-head-copy">
      <div class="page-context">${ui.esc(tr("nav"))}</div>
      <h1>${ui.esc(tr("title"))}</h1>
      <p class="page-description">${ui.esc(tr("description"))}</p>
    </div></div>
    <section class="form-section" id="pl-channel-section">
      <div class="form-section-header"><div><h3>${ui.esc(tr("channel"))}</h3><p>${ui.esc(tr("channelHint"))}</p></div></div>
      <div id="pl-channel-body">${channelHtml(channels)}</div>
    </section>
    <section class="form-section" id="pl-connections">
      <div class="form-section-header"><div><h3>${ui.esc(tr("connections"))}</h3><p>${ui.esc(tr("connectionsHint"))}</p></div></div>
      <div id="pl-connections-body">${connectionsHtml(connStatus)}</div>
    </section>
    <section class="form-section" id="pl-schedule">
      <div class="form-section-header"><div><h3>${ui.esc(tr("schedule"))}</h3><p>${ui.esc(tr("scheduleHint"))}</p></div></div>
      <div id="pl-schedule-body" class="pl-queue">${scheduleHtml(sched)}</div>
    </section>
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
    ${reschedSectionHtml()}
  </div>`;

  async function refresh(root) {
    const [s, i, r, c, a, cs, sc] = await Promise.all([
      call(inChannel("/api/pipeline/source")),
      call(inChannel("/api/pipeline/items")),
      call(inChannel("/api/pipeline/review")),
      call(inChannel("/api/pipeline/clips")),
      call(inChannel("/api/pipeline/analytics")),
      call(inChannel("/api/google/status")),
      call(inChannel("/api/pipeline/schedule")),
    ]);
    if (!isCurrent()) return;
    root.querySelector("#pl-connections-body").innerHTML = connectionsHtml(cs);
    root.querySelector("#pl-schedule-body").innerHTML = scheduleHtml(sc);
    root.querySelector("#pl-clips-body").innerHTML = clipsHtml(c.clips);
    root.querySelector("#pl-analytics-body").innerHTML = analyticsHtml(a);
    root.querySelector("#pl-source-body").innerHTML = sourceHtml(s);
    root.querySelector("#pl-queue-body").innerHTML = queueHtml(i.items);
    root.querySelector("#pl-review-body").innerHTML = reviewHtml(r);
  }

  function announceConnectResult() {
    const hash = window.location.hash || "";
    const query = new URLSearchParams(hash.includes("?") ? hash.split("?")[1] : "");
    if (!query.has("connected") && !query.has("connect_error")) return;
    if (query.get("connected")) ui.toast(tr("connectedOk", { kind: tr(`conn.${query.get("connected")}`) }), "success");
    else ui.toast(query.get("connect_error") === "denied" ? tr("connectDenied") : tr("connectFailed"), "error");
    try {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#/pipeline`);
    } catch {
      /* the message was shown; the address bar keeps the extra text */
    }
  }

  function mount() {
    const root = document.getElementById("pl-review")?.closest(".pl-page");
    if (!root) return;
    announceConnectResult();
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
          if (button.dataset.keep) button.disabled = false;
        } catch {
          button.disabled = false;
        }
      }
    };
    root.addEventListener("change", (event) => {
      if (event.target.id === "pl-channel") {
        chooseChannel(event.target.value);
        window.location.reload();
        return;
      }
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
      if (button.dataset.connect) {
        const kind = button.dataset.connect;
        run(button, async () => {
          const out = await call("/api/google/connect", {
            method: "POST",
            body: { kind, channel_id: channelId, return_to: window.location.href.split("#")[0] },
          });
          window.location.assign(out.url);
          await new Promise(() => {});
        });
      } else if (button.dataset.disconnect) {
        run(button, async () => {
          await call("/api/google/disconnect", {
            method: "POST",
            body: { kind: button.dataset.disconnect, channel_id: channelId },
          });
          ui.toast(tr("disconnected"), "success");
        });
      } else if (button.id === "pl-schedule-refresh") {
        run(button, async () => {
          await call(inChannel("/api/pipeline/schedule/refresh"), { method: "POST" });
          ui.toast(tr("scheduleReadOk"), "success");
        });
      } else if (button.id === "pl-ch-save") {
        run(button, async () => {
          await call(`/api/pipeline/channels/${encodeURIComponent(channelId)}`, {
            method: "PUT",
            body: {
              name: root.querySelector("#pl-ch-name").value.trim(),
              category: root.querySelector("#pl-ch-category").value,
              strong_ratio: Number(root.querySelector("#pl-ch-strong").value),
              weak_ratio: Number(root.querySelector("#pl-ch-weak").value),
            },
          });
          ui.toast(tr("channelSaved"), "success");
          window.location.reload();
        });
      } else if (button.id === "pl-ch-add") {
        run(button, async () => {
          const created = await call("/api/pipeline/channels", {
            method: "POST",
            body: {
              name: root.querySelector("#pl-new-name").value.trim(),
              category: root.querySelector("#pl-new-category").value,
            },
          });
          chooseChannel(created.id);
          window.location.reload();
        });
      } else if (button.id === "pl-save") {
        const value = root.querySelector("#pl-sheet-url").value.trim();
        run(button, async () => {
          const allow = root.querySelector("#pl-allow-public")?.checked === true;
          await call(inChannel("/api/pipeline/source"), {
            method: "PUT",
            body: { sheet_url: value, allow_public: allow },
          });
          ui.toast(tr("saved"), "success");
        });
      } else if (button.id === "pl-sync") {
        run(button, async () => {
          const result = await call(inChannel("/api/pipeline/sync"), { method: "POST" });
          ui.toast(
            tr("synced", { n: result.added }) + (result.duplicates ? tr("dupSynced", { d: result.duplicates }) : ""),
            "success",
          );
        });
      } else if (button.id === "pl-save-settings") {
        run(button, async () => {
          await call(inChannel("/api/pipeline/settings"), {
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
          const plan = await call(inChannel("/api/pipeline/autopublish/plan"));
          root.querySelector("#pl-plan-body").innerHTML = planBodyHtml(plan);
        });
      } else if (button.id === "pl-run") {
        const ids = button.dataset.ids.split(",").filter(Boolean);
        run(button, async () => {
          const out = await call("/api/pipeline/autopublish/run", {
            method: "POST",
            body: {
              clip_ids: ids,
              confirm: true,
              channel_id: channelId,
              allow_unknown_schedule: root.querySelector("#pl-allow-unknown")?.checked === true,
            },
          });
          const ok = out.results.filter((r) => r.status === "scheduled").length;
          const failed = out.results.filter((r) => r.status === "failed");
          ui.toast(tr("runDone", { ok, bad: out.results.length - ok }), failed.length ? "error" : "success");
          if (failed.length) ui.toast(failed[0].error, "error");
          root.querySelector("#pl-plan-body").innerHTML = "";
        });
      } else if (button.id === "pl-resched-build") {
        run(button, async () => {
          const plan = await call(inChannel("/api/pipeline/reschedule/plan"));
          root.querySelector("#pl-resched-body").innerHTML = reschedPlanHtml(plan);
        });
      } else if (button.id === "pl-resched-run") {
        const items = JSON.parse(button.dataset.items || "[]");
        run(button, async () => {
          const out = await call("/api/pipeline/reschedule/apply", {
            method: "POST",
            body: { items, confirm: true, channel_id: channelId },
          });
          const ok = out.results.filter((r) => r.status === "changed").length;
          const failed = out.results.filter((r) => r.status === "failed");
          ui.toast(tr("reschedDone", { ok, bad: out.results.length - ok }), failed.length ? "error" : "success");
          if (failed.length) ui.toast(failed[0].error, "error");
          root.querySelector("#pl-resched-body").innerHTML = "";
        });
      } else if (button.id === "pl-analytics") {
        run(button, async () => {
          await call(inChannel("/api/pipeline/analytics/refresh"), { method: "POST" });
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
