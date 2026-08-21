import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { coalesce } from "./coalesce";

describe("coalesce", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("collapses a burst into one trailing call", () => {
    const fn = vi.fn();
    const schedule = coalesce(fn, { delayMs: 200, maxWaitMs: 1000 });

    schedule();
    vi.advanceTimersByTime(50);
    schedule();
    vi.advanceTimersByTime(50);
    schedule();
    expect(fn).not.toHaveBeenCalled();

    vi.advanceTimersByTime(200);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  // A continuous event stream must not postpone refreshes forever.
  it("fires at least once per max-wait window under a continuous stream", () => {
    const fn = vi.fn();
    const schedule = coalesce(fn, { delayMs: 200, maxWaitMs: 1000 });

    for (let tick = 0; tick < 12; tick += 1) {
      schedule();
      vi.advanceTimersByTime(100);
    }
    // 1200ms of uninterrupted events: the hard deadline must have fired.
    expect(fn.mock.calls.length).toBeGreaterThanOrEqual(1);
  });

  it("cancel prevents any pending call", () => {
    const fn = vi.fn();
    const schedule = coalesce(fn, { delayMs: 200, maxWaitMs: 1000 });
    schedule().cancel();
    vi.advanceTimersByTime(2000);
    expect(fn).not.toHaveBeenCalled();
  });
});
