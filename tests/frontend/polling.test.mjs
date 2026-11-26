import assert from "node:assert/strict";
import { test } from "node:test";

import { createPoller } from "../../clipping_ai/frontend/js/polling.js";

function fakeClock() {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  return {
    setTimeoutFn(listener, delay) {
      const id = nextId++;
      timers.set(id, { at: now + delay, listener });
      return id;
    },
    clearTimeoutFn(id) {
      timers.delete(id);
    },
    async advance(ms) {
      const target = now + ms;
      while (true) {
        let next = null;
        for (const [id, timer] of timers) {
          if (timer.at <= target && (!next || timer.at < next.timer.at)) {
            next = { id, timer };
          }
        }
        if (!next) break;
        timers.delete(next.id);
        now = next.timer.at;
        next.timer.listener();
        await flush();
      }
      now = target;
      await flush();
    },
    get size() {
      return timers.size;
    },
  };
}

async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
}

function setup(overrides = {}) {
  const clock = fakeClock();
  const listeners = new Set();
  const options = {
    callback: async () => {},
    interval: 100,
    isActive: () => true,
    isHidden: () => false,
    addVisibilityListener: (listener) => listeners.add(listener),
    removeVisibilityListener: (listener) => listeners.delete(listener),
    setTimeoutFn: clock.setTimeoutFn,
    clearTimeoutFn: clock.clearTimeoutFn,
    onError: () => {},
    ...overrides,
  };
  return {
    clock,
    listeners,
    start: () => createPoller(options),
    show() {
      for (const listener of listeners) listener();
    },
  };
}

test("polls while active and stops after the work becomes idle", async () => {
  let calls = 0;
  let active = true;
  const harness = setup({
    callback: async () => {
      calls += 1;
    },
    isActive: () => active,
  });
  const poller = harness.start();

  await harness.clock.advance(99);
  assert.equal(calls, 0);
  await harness.clock.advance(1);
  assert.equal(calls, 1);
  await harness.clock.advance(100);
  assert.equal(calls, 2);

  active = false;
  await harness.clock.advance(100);
  assert.equal(calls, 2);
  assert.equal(harness.clock.size, 0);
  poller.stop();
});

test("does not schedule an idle poll loop and refreshes once when visible", async () => {
  let calls = 0;
  const harness = setup({
    callback: async () => {
      calls += 1;
    },
    isActive: () => false,
  });
  const poller = harness.start();

  assert.equal(harness.clock.size, 0);
  await harness.clock.advance(1000);
  assert.equal(calls, 0);

  harness.show();
  await flush();
  assert.equal(calls, 1);
  assert.equal(harness.clock.size, 0);
  poller.stop();
});

test("pauses while hidden, aborts in-flight work, and refreshes on return", async () => {
  let calls = 0;
  let hidden = false;
  let signal = null;
  let release;
  const harness = setup({
    callback: async (context) => {
      calls += 1;
      signal = context.signal;
      if (calls === 1) {
        await new Promise((resolve) => {
          release = resolve;
        });
      }
    },
    isActive: () => true,
    isHidden: () => hidden,
  });
  const poller = harness.start();

  await harness.clock.advance(100);
  assert.equal(calls, 1);
  assert.equal(signal.aborted, false);

  hidden = true;
  harness.show();
  assert.equal(signal.aborted, true);
  release();
  await flush();
  await harness.clock.advance(500);
  assert.equal(calls, 1);

  hidden = false;
  harness.show();
  await flush();
  assert.equal(calls, 2);
  poller.stop();
});

test("cleanup stops timers, aborts requests, and removes the visibility listener", async () => {
  let release;
  let signal = null;
  const harness = setup({
    callback: async (context) => {
      signal = context.signal;
      await new Promise((resolve) => {
        release = resolve;
      });
    },
    isActive: () => true,
  });
  const poller = harness.start();

  await harness.clock.advance(100);
  assert.ok(signal);
  poller.stop();
  assert.equal(signal.aborted, true);
  assert.equal(harness.listeners.size, 0);

  release();
  await flush();
  assert.equal(harness.clock.size, 0);
});

test("never overlaps requests", async () => {
  let calls = 0;
  let release;
  const harness = setup({
    callback: async () => {
      calls += 1;
      await new Promise((resolve) => {
        release = resolve;
      });
    },
  });
  const poller = harness.start();

  const first = poller.tick({ force: true });
  await flush();
  await poller.tick({ force: true });
  assert.equal(calls, 1);

  release();
  await first;
  assert.equal(poller.running, false);
  poller.stop();
});

test("backs off after failures instead of retrying immediately", async () => {
  let calls = 0;
  let fail = true;
  const harness = setup({
    callback: async () => {
      calls += 1;
      if (fail) throw new Error("temporary failure");
    },
  });
  const poller = harness.start();

  await harness.clock.advance(100);
  assert.equal(calls, 1);
  await harness.clock.advance(100);
  assert.equal(calls, 1);
  await harness.clock.advance(100);
  assert.equal(calls, 2);

  fail = false;
  await harness.clock.advance(399);
  assert.equal(calls, 2);
  await harness.clock.advance(1);
  assert.equal(calls, 3);
  await harness.clock.advance(100);
  assert.equal(calls, 4);
  poller.stop();
});
