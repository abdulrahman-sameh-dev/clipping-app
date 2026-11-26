import assert from "node:assert/strict";
import { test } from "node:test";

import {
  TrimDraftStore,
  formatTimecode,
  parseTimecode,
  roundTime,
  trimErrorKey,
} from "../../clipping_ai/frontend/js/trim-model.js";

const highlights = [
  { title: "First", start_time: 10, end_time: 20 },
  { title: "Second", start_time: 30, end_time: 45.5 },
];

function store(overrides = {}) {
  return new TrimDraftStore(highlights, {
    minDuration: 1,
    maxDuration: 30,
    sourceDuration: 100,
    ...overrides,
  });
}

test("formats and parses timecodes to millisecond precision", () => {
  assert.equal(formatTimecode(0), "00:00:00.000");
  assert.equal(formatTimecode(3661.5), "01:01:01.500");
  assert.equal(formatTimecode(-1), "—");
  assert.equal(formatTimecode("nope"), "—");
  assert.equal(roundTime(1.23456), 1.235);

  assert.equal(parseTimecode("00:01:02.345"), 62.345);
  assert.equal(parseTimecode("1:02"), 62);
  assert.equal(parseTimecode("90"), 90);
  assert.equal(parseTimecode("  12.5  "), 12.5);
  assert.equal(parseTimecode("1:60"), null);
  assert.equal(parseTimecode("1:02:60"), null);
  assert.equal(parseTimecode("not-a-time"), null);
  assert.equal(parseTimecode(""), null);
  assert.equal(parseTimecode("1::2"), null);
});

test("seeds detected and draft ranges without mutating the highlights", () => {
  const drafts = store();
  assert.deepEqual(drafts.get(0), { start: 10, end: 20 });
  assert.deepEqual(drafts.detected(1), { start: 30, end: 45.5 });
  assert.equal(drafts.titleFor(0), "First");
  assert.equal(drafts.isCustom(0), false);
  assert.equal(highlights[0].start_time, 10);
});

test("clamps start and end around the other edge and duration limits", () => {
  const drafts = store();
  assert.deepEqual(drafts.clampStart(0, 99), { start: 19, end: 20 });
  assert.deepEqual(drafts.clampEnd(0, -5), { start: 19, end: 20 });
  assert.deepEqual(drafts.nudgeStart(0, -100), { start: 0, end: 20 });
  assert.deepEqual(drafts.nudgeEnd(0, 100), { start: 0, end: 30 });

  const capped = store({ maxDuration: 5, sourceDuration: null });
  capped.setRange(0, 10, 20);
  assert.deepEqual(capped.clampEnd(0, 30), { start: 10, end: 15 });
  assert.deepEqual(capped.clampStart(0, 0), { start: 10, end: 15 });

  const bounded = store({ maxDuration: Infinity, sourceDuration: 12 });
  bounded.setRange(0, 10, 11);
  assert.deepEqual(bounded.clampEnd(0, 30), { start: 10, end: 12 });

  const short = new TrimDraftStore(
    [{ title: "Short", start_time: 0, end_time: 0.05 }],
    { minDuration: 1, sourceDuration: 10 },
  );
  assert.deepEqual(short.clampStart(0, -10), { start: 0, end: 0.05 });
});

test("tracks custom ranges and resets only the requested highlight", () => {
  const drafts = store();
  assert.deepEqual(drafts.clampEnd(0, 21), { start: 10, end: 21 });
  assert.equal(drafts.isCustom(0), true);
  assert.equal(drafts.isCustom(1), false);
  assert.equal(drafts.hasCustomRanges([0, 1]), true);
  assert.equal(drafts.hasCustomRanges([1]), false);

  assert.deepEqual(drafts.reset(0), { start: 10, end: 20 });
  assert.equal(drafts.isCustom(0), false);
  assert.deepEqual(drafts.get(1), { start: 30, end: 45.5 });
});

test("validates order, duration, and source bounds", () => {
  const drafts = store({ maxDuration: 10, minDuration: 2, sourceDuration: 100 });
  assert.equal(drafts.validateValues(10, 12), null);
  assert.equal(drafts.validateValues(5, 5), "order");
  assert.equal(drafts.validateValues(-1, 4), "bounds");
  assert.equal(drafts.validateValues(10, 11), "too-short");
  assert.equal(drafts.validateValues(0, 12), "too-long");
  assert.equal(drafts.validateValues(null, 12), "bounds");

  const bounded = store({ maxDuration: Infinity, minDuration: 1, sourceDuration: 12 });
  assert.equal(bounded.validateValues(1, 12.1), "source");

  const result = bounded.validate([0, 1]);
  assert.equal(result.ok, false);
  assert.equal(result.index, 0);
  assert.equal(result.code, "source");
  assert.deepEqual(drafts.validate([0]), { ok: true });
});

test("builds only valid ranges in selection order", () => {
  const drafts = store();
  drafts.clampEnd(1, 44);
  drafts.setRange(0, 10.5, 18.25);
  assert.deepEqual(drafts.rangesFor([1, 0, 99]), [
    { highlight_index: 1, start_time: 30, end_time: 44 },
    { highlight_index: 0, start_time: 10.5, end_time: 18.25 },
  ]);
});

test("updates a discovered source duration without allowing zero", () => {
  const drafts = store({ sourceDuration: null });
  assert.equal(drafts.sourceDuration, null);
  drafts.setSourceDuration(125.5);
  assert.equal(drafts.sourceDuration, 125.5);
  drafts.setSourceDuration(0);
  assert.equal(drafts.sourceDuration, 125.5);
});

test("maps validation codes to translation keys", () => {
  assert.equal(trimErrorKey("order"), "trim.errorOrder");
  assert.equal(trimErrorKey("too-long"), "trim.errorMax");
  assert.equal(trimErrorKey("unknown"), "trim.errorUnknown");
});
