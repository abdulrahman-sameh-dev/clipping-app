export function createPoller({
  callback,
  interval = 4000,
  idleInterval = null,
  isActive = () => true,
  isHidden = () => Boolean(globalThis.document?.hidden),
  addVisibilityListener = (listener) =>
    globalThis.document?.addEventListener("visibilitychange", listener),
  removeVisibilityListener = (listener) =>
    globalThis.document?.removeEventListener("visibilitychange", listener),
  setTimeoutFn = (listener, delay) => globalThis.setTimeout(listener, delay),
  clearTimeoutFn = (timer) => globalThis.clearTimeout(timer),
  onError = (error) => console.error(error),
  maxBackoff = 30000,
} = {}) {
  let stopped = false;
  let running = false;
  let timer = null;
  let failures = 0;
  let controller = null;
  let forceRequested = false;

  function baseDelay() {
    if (isHidden()) return null;
    return isActive() ? interval : idleInterval;
  }

  function clearTimer() {
    if (timer === null) return;
    clearTimeoutFn(timer);
    timer = null;
  }

  function schedule() {
    if (stopped || running || timer !== null) return;
    const base = baseDelay();
    if (base === null) return;
    const delay = failures ? Math.min(base * 2 ** failures, maxBackoff) : base;
    timer = setTimeoutFn(() => {
      timer = null;
      void tick();
    }, delay);
  }

  async function tick({ force = false } = {}) {
    if (stopped || running) return;
    if (!force) {
      if (isHidden() || baseDelay() === null) {
        clearTimer();
        return;
      }
    }
    running = true;
    controller = new AbortController();
    try {
      await callback({ signal: controller.signal, force });
      failures = 0;
    } catch (error) {
      if (error?.name !== "AbortError") {
        failures += 1;
        onError(error);
      }
    } finally {
      running = false;
      controller = null;
      if (forceRequested) {
        forceRequested = false;
        if (!stopped && !isHidden()) {
          void tick({ force: true });
          return;
        }
      }
      schedule();
    }
  }

  function handleVisibility() {
    if (stopped) return;
    if (isHidden()) {
      forceRequested = false;
      clearTimer();
      controller?.abort();
      return;
    }
    failures = 0;
    if (running) {
      forceRequested = true;
      return;
    }
    void tick({ force: true });
  }

  addVisibilityListener(handleVisibility);
  schedule();

  return {
    tick,
    stop() {
      if (stopped) return;
      stopped = true;
      forceRequested = false;
      clearTimer();
      controller?.abort();
      removeVisibilityListener(handleVisibility);
    },
    get running() {
      return running;
    },
  };
}
