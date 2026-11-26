import { t } from "./i18n.js";
import {
  TrimDraftStore,
  formatTimecode,
  parseTimecode,
  roundTime,
  trimErrorKey,
} from "./trim-model.js";
import { esc, icon } from "./ui.js";

export {
  TrimDraftStore,
  formatTimecode,
  parseTimecode,
  roundTime,
  trimErrorKey,
} from "./trim-model.js";

const KEYBOARD_STEP = 0.1;
const KEYBOARD_LARGE_STEP = 1;

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function createTrimEditor({
  root,
  highlights = [],
  sourceUrl,
  sourceDuration = null,
  minDuration = 0.1,
  maxDuration = Infinity,
  initialIndex = null,
  onChange = () => {},
  onActiveChange = () => {},
}) {
  if (!root) throw new Error("Trim editor requires a root element.");
  return new TrimEditor({
    root,
    highlights,
    sourceUrl,
    sourceDuration,
    minDuration,
    maxDuration,
    initialIndex,
    onChange,
    onActiveChange,
  });
}

class TrimEditor {
  constructor({
    root,
    highlights,
    sourceUrl,
    sourceDuration,
    minDuration,
    maxDuration,
    initialIndex,
    onChange,
    onActiveChange,
  }) {
    this.root = root;
    this.sourceUrl = sourceUrl;
    this.initialDuration = finiteNumber(sourceDuration);
    this.store = new TrimDraftStore(highlights, {
      minDuration,
      maxDuration,
      sourceDuration,
    });
    this.onChange = onChange;
    this.onActiveChange = onActiveChange;
    this.abortController = new AbortController();
    this.activeIndex = null;
    this.selectedPlayback = false;
    this.animationFrame = null;
    this.dragging = null;
    this.pendingSeek = null;
    this.hasMetadata = false;
    this.mediaError = false;
    this.destroyed = false;

    this.renderTemplate();
    this.cacheElements();
    this.bindEvents();
    this.setActive(initialIndex, { notify: false });
    if (sourceUrl) {
      this.video.src = sourceUrl;
      this.video.load();
    } else {
      this.showPreviewError();
    }
  }

  renderTemplate() {
    this.root.innerHTML = `<div class="trim-editor-head">
      <div>
        <h3 id="trim-editor-title">${esc(t("trim.title"))}</h3>
        <p>${esc(t("trim.description"))}</p>
      </div>
      <span class="tag is-teal" id="trim-state-tag">${esc(t("trim.detected"))}</span>
    </div>
    <div class="trim-editor-grid" id="trim-editor-grid">
      <div class="trim-preview">
        <video class="trim-video" id="trim-video" controls playsinline preload="metadata" aria-label="${esc(
          t("trim.sourcePreview"),
        )}"></video>
        <div class="trim-preview-status" id="trim-preview-status" role="status" aria-live="polite">
          <span class="loading-mark" aria-hidden="true"></span>
          <span>${esc(t("trim.videoLoading"))}</span>
        </div>
      </div>
      <div class="trim-panel">
        <div class="trim-active">
          <span class="tag is-accent" id="trim-active-index">01</span>
          <div>
            <span class="trim-active-label">${esc(t("trim.activeLabel"))}</span>
            <strong id="trim-active-title"></strong>
          </div>
        </div>
        <div class="trim-timeline">
          <div class="trim-track" id="trim-track" dir="ltr">
            <div class="trim-detected" id="trim-detected" aria-hidden="true"></div>
            <div class="trim-selection" id="trim-selection" aria-hidden="true"></div>
            <div class="trim-playhead" id="trim-playhead" aria-hidden="true"></div>
            <button class="trim-handle trim-handle-start" id="trim-start-handle" type="button" role="slider" aria-orientation="horizontal" aria-label="${esc(
              t("trim.start"),
            )}" data-trim-handle="start"><span aria-hidden="true"></span></button>
            <button class="trim-handle trim-handle-end" id="trim-end-handle" type="button" role="slider" aria-orientation="horizontal" aria-label="${esc(
              t("trim.end"),
            )}" data-trim-handle="end"><span aria-hidden="true"></span></button>
          </div>
          <div class="trim-scale" dir="ltr">
            <span>${formatTimecode(0)}</span>
            <span id="trim-playhead-time">${formatTimecode(0)}</span>
            <span id="trim-total">${formatTimecode(0)}</span>
          </div>
          <div class="trim-legend">
            <span><i class="is-detected" aria-hidden="true"></i>${esc(
              t("trim.detectedRange"),
            )}</span>
            <span><i class="is-selected" aria-hidden="true"></i>${esc(
              t("trim.selectedRange"),
            )}</span>
          </div>
        </div>
        <div class="trim-fields">
          <label>${esc(t("trim.start"))}
            <input id="trim-start-input" type="text" inputmode="decimal" dir="ltr" autocomplete="off" spellcheck="false" placeholder="00:00:00.000" />
          </label>
          <label>${esc(t("trim.end"))}
            <input id="trim-end-input" type="text" inputmode="decimal" dir="ltr" autocomplete="off" spellcheck="false" placeholder="00:00:00.000" />
          </label>
        </div>
        <div class="trim-meta">
          <span>${esc(t("trim.clipLength"))} <strong id="trim-duration"></strong></span>
          <span id="trim-limits"></span>
        </div>
        <div class="trim-actions">
          <button class="button button-primary button-small" id="trim-play" type="button">${icon(
            "play",
          )}<span>${esc(t("trim.playSelection"))}</span></button>
          <button class="button button-secondary button-small" id="trim-pause" type="button"><span>${esc(
            t("trim.pause"),
          )}</span></button>
          <button class="button button-secondary button-small" id="trim-resume" type="button"><span>${esc(
            t("trim.resume"),
          )}</span></button>
          <button class="button button-ghost button-small" id="trim-reset" type="button">${icon(
            "retry",
          )}<span>${esc(t("trim.reset"))}</span></button>
        </div>
        <p class="field-error trim-error" id="trim-error" role="alert" hidden></p>
      </div>
    </div>
    <div class="trim-empty" id="trim-empty" hidden>
      ${icon("sliders")}
      <strong>${esc(t("trim.selectTitle"))}</strong>
      <span>${esc(t("trim.selectHint"))}</span>
    </div>`;
  }

  cacheElements() {
    const query = (selector) => this.root.querySelector(selector);
    this.grid = query("#trim-editor-grid");
    this.empty = query("#trim-empty");
    this.video = query("#trim-video");
    this.previewStatus = query("#trim-preview-status");
    this.activeIndexTag = query("#trim-active-index");
    this.activeTitle = query("#trim-active-title");
    this.stateTag = query("#trim-state-tag");
    this.track = query("#trim-track");
    this.detectedBar = query("#trim-detected");
    this.selectionBar = query("#trim-selection");
    this.playhead = query("#trim-playhead");
    this.playheadTime = query("#trim-playhead-time");
    this.total = query("#trim-total");
    this.startHandle = query("#trim-start-handle");
    this.endHandle = query("#trim-end-handle");
    this.startInput = query("#trim-start-input");
    this.endInput = query("#trim-end-input");
    this.duration = query("#trim-duration");
    this.limits = query("#trim-limits");
    this.error = query("#trim-error");
    this.playButton = query("#trim-play");
    this.pauseButton = query("#trim-pause");
    this.resumeButton = query("#trim-resume");
    this.resetButton = query("#trim-reset");
  }

  bindEvents() {
    const { signal } = this.abortController;
    const options = { signal };
    this.video.addEventListener(
      "loadedmetadata",
      () => {
        this.hasMetadata = true;
        this.mediaError = false;
        const duration = finiteNumber(this.video.duration);
        if (duration !== null && duration > 0) this.store.setSourceDuration(duration);
        this.previewStatus.hidden = true;
        this.previewStatus.classList.remove("is-error");
        if (this.pendingSeek !== null) {
          this.seekTo(this.pendingSeek);
        }
        this.paint();
      },
      options,
    );
    this.video.addEventListener("durationchange", () => this.paint(), options);
    this.video.addEventListener(
      "error",
      () => {
        this.mediaError = true;
        this.showPreviewError();
        this.paint();
      },
      options,
    );
    this.video.addEventListener("timeupdate", () => this.paintPlayhead(this.video.currentTime), options);
    this.video.addEventListener("seeked", () => this.paintPlayhead(this.video.currentTime), options);
    this.video.addEventListener("ended", () => this.stopSelectedPlayback(), options);
    this.video.addEventListener("pause", () => this.stopSelectedPlayback(), options);
    this.video.addEventListener(
      "play",
      () => {
        if (this.selectedPlayback) this.startTicker();
      },
      options,
    );

    this.startInput.addEventListener(
      "input",
      () => this.clearError(),
      options,
    );
    this.endInput.addEventListener(
      "input",
      () => this.clearError(),
      options,
    );
    this.startInput.addEventListener(
      "change",
      () => this.applyInput("start"),
      options,
    );
    this.endInput.addEventListener(
      "change",
      () => this.applyInput("end"),
      options,
    );
    this.startInput.addEventListener(
      "keydown",
      (event) => this.handleInputKeydown(event, "start"),
      options,
    );
    this.endInput.addEventListener(
      "keydown",
      (event) => this.handleInputKeydown(event, "end"),
      options,
    );

    for (const [edge, handle] of [
      ["start", this.startHandle],
      ["end", this.endHandle],
    ]) {
      handle.addEventListener("keydown", (event) => this.handleHandleKeydown(edge, event), options);
      handle.addEventListener("pointerdown", (event) => this.beginDrag(edge, event), options);
      handle.addEventListener("pointermove", (event) => this.moveDrag(event), options);
      handle.addEventListener("pointerup", (event) => this.endDrag(event, true), options);
      handle.addEventListener("pointercancel", (event) => this.endDrag(event, true), options);
      handle.addEventListener("lostpointercapture", (event) => this.endDrag(event, false), options);
    }

    this.playButton.addEventListener("click", () => this.playSelection(false), options);
    this.pauseButton.addEventListener("click", () => this.pauseSelection(), options);
    this.resumeButton.addEventListener("click", () => this.playSelection(true), options);
    this.resetButton.addEventListener("click", () => this.resetActive(), options);
  }

  open(index) {
    if (this.destroyed) return;
    this.setActive(index, { notify: true });
  }

  getActiveIndex() {
    return this.activeIndex;
  }

  setActive(index, { notify = true } = {}) {
    if (this.destroyed) return;
    const candidate =
      index === null || index === undefined || index === "" ? null : Number(index);
    const nextIndex =
      candidate !== null && this.store.entry(candidate) ? candidate : null;
    if (nextIndex === this.activeIndex) {
      if (nextIndex === null) this.paint();
      return;
    }
    this.pauseSelection();
    this.activeIndex = nextIndex;
    this.clearError();
    const hasActive = nextIndex !== null;
    this.grid.hidden = !hasActive;
    this.empty.hidden = hasActive;
    this.root.classList.toggle("is-empty", !hasActive);
    if (hasActive) {
      const range = this.store.get(nextIndex);
      this.activeIndexTag.textContent = String(nextIndex + 1).padStart(2, "0");
      this.activeTitle.textContent = this.store.titleFor(nextIndex);
      this.seekTo(range.start);
    } else {
      this.activeTitle.textContent = "";
    }
    this.paint();
    if (notify) this.onActiveChange(this.activeIndex);
  }

  timelineDuration() {
    if (this.store.sourceDuration !== null) return this.store.sourceDuration;
    if (this.initialDuration !== null && this.initialDuration > 0) {
      return roundTime(this.initialDuration);
    }
    return Math.max(1, this.store.maxEnd(), this.store.minDuration);
  }

  percentage(time, duration = this.timelineDuration()) {
    if (!(duration > 0)) return 0;
    return clamp((finiteNumber(time) ?? 0) / duration, 0, 1) * 100;
  }

  styleRange(element, start, end, duration) {
    const left = this.percentage(start, duration);
    const right = this.percentage(end, duration);
    element.style.insetInlineStart = `${left}%`;
    element.style.width = `${Math.max(0, right - left)}%`;
  }

  paint() {
    if (this.destroyed) return;
    const index = this.activeIndex;
    const duration = this.timelineDuration();
    this.total.textContent = formatTimecode(duration);
    if (index === null) {
      this.stateTag.textContent = t("trim.selectTitle");
      this.stateTag.classList.remove("is-accent");
      this.stateTag.classList.add("is-teal");
      return;
    }
    const entry = this.store.entry(index);
    const range = this.store.get(index);
    const detected = this.store.detected(index);
    this.styleRange(this.detectedBar, detected.start, detected.end, duration);
    this.styleRange(this.selectionBar, range.start, range.end, duration);
    this.startHandle.style.insetInlineStart = `${this.percentage(range.start, duration)}%`;
    this.endHandle.style.insetInlineStart = `${this.percentage(range.end, duration)}%`;
    if (document.activeElement !== this.startInput) {
      this.startInput.value = formatTimecode(range.start);
    }
    if (document.activeElement !== this.endInput) {
      this.endInput.value = formatTimecode(range.end);
    }
    this.duration.textContent = formatTimecode(range.end - range.start);
    this.limits.textContent = t("trim.limits", {
      min: formatTimecode(this.store.minDuration),
      max: Number.isFinite(this.store.maxDuration)
        ? formatTimecode(this.store.maxDuration)
        : "—",
    });
    const custom = this.store.isCustom(index);
    this.stateTag.textContent = custom ? t("trim.custom") : t("trim.detected");
    this.stateTag.classList.toggle("is-accent", custom);
    this.stateTag.classList.toggle("is-teal", !custom);
    this.updateHandleAria("start", range, duration);
    this.updateHandleAria("end", range, duration);
    const controlsDisabled = this.mediaError || !this.hasMetadata;
    this.playButton.disabled = controlsDisabled;
    this.pauseButton.disabled = controlsDisabled;
    this.resumeButton.disabled = controlsDisabled;
    this.paintPlayhead(this.video.currentTime);
    this.startHandle.title = `${t("trim.start")} ${formatTimecode(range.start)}`;
    this.endHandle.title = `${t("trim.end")} ${formatTimecode(range.end)}`;
    this.activeTitle.textContent = entry.title;
  }

  updateHandleAria(edge, range, duration) {
    const handle = edge === "start" ? this.startHandle : this.endHandle;
    const value = edge === "start" ? range.start : range.end;
    const minimum =
      edge === "start" ? 0 : Math.max(0, roundTime(range.start + this.store.minDuration));
    const maximum =
      edge === "start"
        ? Math.max(0, Math.min(duration, roundTime(range.end - this.store.minDuration)))
        : duration;
    handle.setAttribute("aria-valuemin", String(minimum));
    handle.setAttribute("aria-valuemax", String(maximum));
    handle.setAttribute("aria-valuenow", String(roundTime(value)));
    handle.setAttribute("aria-valuetext", formatTimecode(value));
  }

  paintPlayhead(value) {
    const duration = this.timelineDuration();
    const current = clamp(finiteNumber(value) ?? 0, 0, duration);
    this.playhead.style.insetInlineStart = `${this.percentage(current, duration)}%`;
    this.playheadTime.textContent = formatTimecode(current);
  }

  seekTo(value) {
    const time = Math.max(0, finiteNumber(value) ?? 0);
    this.pendingSeek = time;
    if (!this.hasMetadata) {
      this.paintPlayhead(time);
      return;
    }
    try {
      this.video.currentTime = time;
      this.pendingSeek = null;
    } catch {
      this.pendingSeek = time;
    }
    this.paintPlayhead(time);
  }

  applyInput(edge) {
    if (this.activeIndex === null) return;
    const input = edge === "start" ? this.startInput : this.endInput;
    const parsed = parseTimecode(input.value);
    if (parsed === null) {
      this.setError("format", edge);
      return;
    }
    const range = this.store.get(this.activeIndex);
    const start = edge === "start" ? parsed : range.start;
    const end = edge === "end" ? parsed : range.end;
    const code = this.store.validateValues(start, end);
    if (code) {
      this.setError(code, edge);
      return;
    }
    this.pauseSelection();
    this.store.setRange(this.activeIndex, start, end);
    this.clearError();
    input.value = formatTimecode(edge === "start" ? start : end);
    this.paint();
    this.emitChange();
  }

  handleInputKeydown(event, edge) {
    if (event.key !== "Enter") return;
    event.preventDefault();
    this.applyInput(edge);
  }

  handleHandleKeydown(edge, event) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    this.pauseSelection();
    const step = event.shiftKey ? KEYBOARD_LARGE_STEP : KEYBOARD_STEP;
    const delta = event.key === "ArrowRight" ? step : -step;
    if (edge === "start") this.store.nudgeStart(this.activeIndex, delta);
    else this.store.nudgeEnd(this.activeIndex, delta);
    this.clearError();
    this.paint();
    this.emitChange();
  }

  beginDrag(edge, event) {
    if (this.activeIndex === null) return;
    if (event.button !== undefined && event.button !== 0) return;
    event.preventDefault();
    this.pauseSelection();
    const handle = edge === "start" ? this.startHandle : this.endHandle;
    this.dragging = { edge, pointerId: event.pointerId, moved: false };
    this.root.classList.add("is-dragging");
    handle.classList.add("is-dragging");
    try {
      handle.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture is best-effort.
    }
  }

  moveDrag(event) {
    if (!this.dragging || event.pointerId !== this.dragging.pointerId) return;
    if (event.buttons === 0) {
      this.endDrag(event, true);
      return;
    }
    event.preventDefault();
    const rect = this.track.getBoundingClientRect();
    if (!rect.width) return;
    const ratio = clamp((event.clientX - rect.left) / rect.width, 0, 1);
    const time = ratio * this.timelineDuration();
    const before = this.store.get(this.activeIndex);
    if (this.dragging.edge === "start") this.store.clampStart(this.activeIndex, time);
    else this.store.clampEnd(this.activeIndex, time);
    const after = this.store.get(this.activeIndex);
    if (before && after && (before.start !== after.start || before.end !== after.end)) {
      this.dragging.moved = true;
      this.clearError();
      this.paint();
    }
  }

  endDrag(event, commit) {
    if (!this.dragging) return;
    const { edge, pointerId, moved } = this.dragging;
    this.dragging = null;
    this.root.classList.remove("is-dragging");
    const handle = edge === "start" ? this.startHandle : this.endHandle;
    handle.classList.remove("is-dragging");
    try {
      if (handle.hasPointerCapture?.(pointerId)) handle.releasePointerCapture(pointerId);
    } catch {
      // The pointer may already be released.
    }
    if (commit && moved) this.emitChange();
  }

  playSelection(resume) {
    if (this.activeIndex === null || this.mediaError || !this.hasMetadata) return;
    const code = this.store.validateIndex(this.activeIndex);
    if (code) {
      this.setError(code);
      return;
    }
    const range = this.store.get(this.activeIndex);
    const current = finiteNumber(this.video.currentTime) ?? 0;
    if (!resume || current < range.start || current >= range.end) {
      this.seekTo(range.start);
    }
    this.selectedPlayback = true;
    this.startTicker();
    const result = this.video.play();
    if (result?.catch) {
      result.catch(() => {
        this.stopSelectedPlayback();
        this.setError("playback");
      });
    }
  }

  pauseSelection() {
    this.selectedPlayback = false;
    this.stopTicker();
    try {
      this.video.pause();
    } catch {
      // The media element may not be attached yet.
    }
  }

  stopSelectedPlayback() {
    this.selectedPlayback = false;
    this.stopTicker();
  }

  startTicker() {
    if (this.animationFrame !== null) return;
    const tick = () => {
      this.animationFrame = null;
      if (this.destroyed || !this.selectedPlayback || this.activeIndex === null) return;
      const range = this.store.get(this.activeIndex);
      if (!range) return;
      const current = finiteNumber(this.video.currentTime) ?? range.start;
      if (current + 0.005 >= range.end) {
        this.selectedPlayback = false;
        try {
          this.video.pause();
          this.video.currentTime = range.end;
        } catch {
          // Ignore unsupported seeks.
        }
        this.paintPlayhead(range.end);
        return;
      }
      this.paintPlayhead(current);
      this.animationFrame = window.requestAnimationFrame(tick);
    };
    this.animationFrame = window.requestAnimationFrame(tick);
  }

  stopTicker() {
    if (this.animationFrame === null) return;
    window.cancelAnimationFrame(this.animationFrame);
    this.animationFrame = null;
  }

  resetActive() {
    if (this.activeIndex === null) return;
    this.pauseSelection();
    this.store.reset(this.activeIndex);
    this.clearError();
    this.paint();
    this.emitChange();
  }

  messageFor(code) {
    return t(trimErrorKey(code), {
      min: formatTimecode(this.store.minDuration),
      max: Number.isFinite(this.store.maxDuration)
        ? formatTimecode(this.store.maxDuration)
        : "—",
    });
  }

  setError(code, edge = null) {
    this.error.hidden = false;
    this.error.textContent = this.messageFor(code);
    this.startInput.removeAttribute("aria-invalid");
    this.endInput.removeAttribute("aria-invalid");
    if (edge === "start") this.startInput.setAttribute("aria-invalid", "true");
    if (edge === "end") this.endInput.setAttribute("aria-invalid", "true");
    if (edge === null) {
      this.startInput.setAttribute("aria-invalid", "true");
      this.endInput.setAttribute("aria-invalid", "true");
    }
  }

  clearError() {
    this.error.hidden = true;
    this.error.textContent = "";
    this.startInput.removeAttribute("aria-invalid");
    this.endInput.removeAttribute("aria-invalid");
  }

  showPreviewError() {
    this.previewStatus.hidden = false;
    this.previewStatus.classList.add("is-error");
    this.previewStatus.innerHTML = `${icon("alert")}<span>${esc(
      t("trim.videoUnavailable"),
    )}</span>`;
  }

  revealError(index, code) {
    this.setActive(index, { notify: true });
    this.setError(code);
  }

  emitChange() {
    if (this.activeIndex === null) return;
    this.onChange({
      index: this.activeIndex,
      range: this.store.get(this.activeIndex),
      custom: this.store.isCustom(this.activeIndex),
    });
  }

  commitPendingInput() {
    if (this.activeIndex === null) return { ok: true };
    const start = parseTimecode(this.startInput.value);
    if (start === null) {
      return { ok: false, index: this.activeIndex, code: "format" };
    }
    const end = parseTimecode(this.endInput.value);
    if (end === null) {
      return { ok: false, index: this.activeIndex, code: "format" };
    }
    const code = this.store.validateValues(start, end);
    if (code) return { ok: false, index: this.activeIndex, code };
    const current = this.store.get(this.activeIndex);
    if (current.start !== start || current.end !== end) {
      this.store.setRange(this.activeIndex, start, end);
      this.paint();
    }
    this.clearError();
    return { ok: true };
  }

  validate(indices) {
    const pending = this.commitPendingInput();
    if (!pending.ok) {
      return { ...pending, message: this.messageFor(pending.code) };
    }
    const result = this.store.validate(indices);
    return result.ok ? result : { ...result, message: this.messageFor(result.code) };
  }

  rangesFor(indices) {
    return this.store.rangesFor(indices);
  }

  hasCustomRanges(indices) {
    return this.store.hasCustomRanges(indices);
  }

  isCustom(index) {
    return this.store.isCustom(Number(index));
  }

  rangeFor(index) {
    return this.store.get(Number(index));
  }

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.stopSelectedPlayback();
    this.abortController.abort();
    this.dragging = null;
    try {
      this.video.pause();
      this.video.removeAttribute("src");
      this.video.load();
    } catch {
      // Ignore teardown failures for detached media.
    }
    this.root.innerHTML = "";
    this.root.classList.remove("is-empty", "is-dragging");
  }
}
