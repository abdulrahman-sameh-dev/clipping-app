const ROUNDING = 1000;
const CUSTOM_EPSILON = 0.0005;
const DURATION_EPSILON = 1e-6;
const SOURCE_TOLERANCE = 0.05;

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function roundTime(value) {
  const number = finiteNumber(value);
  if (number === null) return null;
  return Math.round(number * ROUNDING) / ROUNDING;
}

export function formatTimecode(seconds) {
  const value = finiteNumber(seconds);
  if (value === null || value < 0) return "—";
  const totalMs = Math.round(value * ROUNDING);
  const hours = Math.floor(totalMs / 3600000);
  const minutes = Math.floor((totalMs % 3600000) / 60000);
  const secs = Math.floor((totalMs % 60000) / 1000);
  const ms = totalMs % 1000;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(
    secs,
  ).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
}

export function parseTimecode(value) {
  const text = String(value ?? "").trim();
  if (!text) return null;
  if (!text.includes(":")) {
    if (!/^\d+(?:\.\d+)?$/.test(text)) return null;
    const seconds = Number(text);
    return Number.isFinite(seconds) ? roundTime(seconds) : null;
  }
  const parts = text.split(":");
  if (parts.length < 2 || parts.length > 3 || parts.some((part) => part === "")) return null;
  const secondsPart = parts.at(-1);
  if (!/^\d+(?:\.\d+)?$/.test(secondsPart)) return null;
  if (parts.slice(0, -1).some((part) => !/^\d+$/.test(part))) return null;
  const seconds = Number(secondsPart);
  if (seconds >= 60) return null;
  if (parts.length === 2) {
    const minutes = Number(parts[0]);
    return roundTime(minutes * 60 + seconds);
  }
  const hours = Number(parts[0]);
  const minutes = Number(parts[1]);
  if (minutes >= 60) return null;
  return roundTime(hours * 3600 + minutes * 60 + seconds);
}

export function trimErrorKey(code) {
  return (
    {
      format: "trim.errorFormat",
      bounds: "trim.errorBounds",
      order: "trim.errorOrder",
      "too-short": "trim.errorMin",
      "too-long": "trim.errorMax",
      source: "trim.errorSource",
      playback: "trim.errorPlayback",
    }[code] || "trim.errorUnknown"
  );
}

export class TrimDraftStore {
  constructor(
    highlights = [],
    { minDuration = 0.1, maxDuration = Infinity, sourceDuration = null } = {},
  ) {
    this.minDuration = Math.max(0, finiteNumber(minDuration) ?? 0.1);
    const max = finiteNumber(maxDuration);
    this.maxDuration = max !== null && max > 0 ? max : Infinity;
    this.sourceDuration = this.normalizeDuration(sourceDuration);
    this.entries = (highlights || []).map((highlight, index) => {
      const rawStart = finiteNumber(highlight?.start_time);
      const rawEnd = finiteNumber(highlight?.end_time);
      const start = roundTime(Math.max(0, rawStart ?? 0));
      const end = roundTime(Math.max(start, rawEnd ?? start));
      const detected = Object.freeze({ start, end });
      return {
        index,
        title: String(highlight?.title || ""),
        detected,
        draft: { ...detected },
      };
    });
  }

  normalizeDuration(value) {
    const number = finiteNumber(value);
    return number !== null && number > 0 ? roundTime(number) : null;
  }

  entry(index) {
    return Number.isInteger(index) ? this.entries[index] || null : null;
  }

  get(index) {
    const entry = this.entry(index);
    return entry ? { ...entry.draft } : null;
  }

  detected(index) {
    const entry = this.entry(index);
    return entry ? { ...entry.detected } : null;
  }

  titleFor(index) {
    return this.entry(index)?.title || "";
  }

  setSourceDuration(value) {
    const duration = this.normalizeDuration(value);
    if (duration !== null) this.sourceDuration = duration;
  }

  maxEnd() {
    return this.entries.reduce(
      (maximum, entry) => Math.max(maximum, entry.draft.end, entry.detected.end),
      0,
    );
  }

  isCustom(index) {
    const entry = this.entry(index);
    if (!entry) return false;
    return (
      Math.abs(entry.draft.start - entry.detected.start) > CUSTOM_EPSILON ||
      Math.abs(entry.draft.end - entry.detected.end) > CUSTOM_EPSILON
    );
  }

  hasCustomRanges(indices) {
    return [...indices].some((index) => this.isCustom(Number(index)));
  }

  reset(index) {
    const entry = this.entry(index);
    if (!entry) return null;
    entry.draft = { ...entry.detected };
    return this.get(index);
  }

  clampStart(index, value) {
    const entry = this.entry(index);
    const number = finiteNumber(value);
    if (!entry || number === null) return null;
    const sourceLimit = this.sourceDuration ?? Infinity;
    const end = entry.draft.end;
    let upper = Math.max(
      0,
      Math.min(end, end - this.minDuration, sourceLimit),
    );
    let lower = Math.max(
      0,
      Number.isFinite(this.maxDuration) ? end - this.maxDuration : 0,
    );
    if (lower > upper) lower = upper;
    if (sourceLimit < lower) lower = sourceLimit;
    if (sourceLimit < upper) upper = sourceLimit;
    entry.draft = {
      start: roundTime(clamp(number, Math.min(lower, upper), Math.max(lower, upper))),
      end,
    };
    return this.get(index);
  }

  clampEnd(index, value) {
    const entry = this.entry(index);
    const number = finiteNumber(value);
    if (!entry || number === null) return null;
    const sourceLimit = this.sourceDuration ?? Infinity;
    const start = entry.draft.start;
    let lower = start + this.minDuration;
    let upper = Number.isFinite(this.maxDuration)
      ? start + this.maxDuration
      : sourceLimit;
    upper = Math.min(upper, sourceLimit);
    if (lower > upper) lower = upper;
    entry.draft = {
      start,
      end: roundTime(clamp(number, Math.min(lower, upper), Math.max(lower, upper))),
    };
    return this.get(index);
  }

  nudgeStart(index, delta) {
    const range = this.get(index);
    if (!range) return null;
    return this.clampStart(index, range.start + Number(delta));
  }

  nudgeEnd(index, delta) {
    const range = this.get(index);
    if (!range) return null;
    return this.clampEnd(index, range.end + Number(delta));
  }

  setRange(index, start, end) {
    const entry = this.entry(index);
    const nextStart = roundTime(start);
    const nextEnd = roundTime(end);
    if (!entry || nextStart === null || nextEnd === null) return null;
    entry.draft = { start: nextStart, end: nextEnd };
    return this.get(index);
  }

  validateValues(start, end) {
    const nextStart = finiteNumber(start);
    const nextEnd = finiteNumber(end);
    if (nextStart === null || nextEnd === null || nextStart < 0 || nextEnd < 0) {
      return "bounds";
    }
    if (nextEnd <= nextStart) return "order";
    const duration = nextEnd - nextStart;
    if (duration + DURATION_EPSILON < this.minDuration) return "too-short";
    if (duration - DURATION_EPSILON > this.maxDuration) return "too-long";
    if (
      this.sourceDuration !== null &&
      nextEnd > this.sourceDuration + SOURCE_TOLERANCE
    ) {
      return "source";
    }
    return null;
  }

  validateIndex(index) {
    const range = this.get(index);
    return range ? this.validateValues(range.start, range.end) : "bounds";
  }

  validate(indices) {
    for (const index of indices) {
      const number = Number(index);
      const code = this.validateIndex(number);
      if (code) return { ok: false, index: number, code };
    }
    return { ok: true };
  }

  rangesFor(indices) {
    return [...indices]
      .map((index) => {
        const number = Number(index);
        const range = this.get(number);
        return range
          ? {
              highlight_index: number,
              start_time: range.start,
              end_time: range.end,
            }
          : null;
      })
      .filter(Boolean);
  }
}
