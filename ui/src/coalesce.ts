// Coalescing scheduler for event-driven refreshes.
//
// The project SSE stream can emit many graph-mutating events per second
// (status transitions, harness events, work-item updates). Reacting to each
// one with a full graph fetch creates a request storm that saturates the
// server and delays the very events being refreshed. This helper collapses a
// burst into one trailing call, with a bounded maximum wait so a continuous
// event stream still refreshes periodically.

export interface CoalescedCall {
  cancel(): void;
}

export function coalesce(
  fn: () => void,
  { delayMs, maxWaitMs }: { delayMs: number; maxWaitMs: number },
): () => CoalescedCall {
  let softTimer: number | null = null;
  let hardTimer: number | null = null;

  const clear = () => {
    if (softTimer !== null) {
      window.clearTimeout(softTimer);
      softTimer = null;
    }
    if (hardTimer !== null) {
      window.clearTimeout(hardTimer);
      hardTimer = null;
    }
  };

  const fire = () => {
    clear();
    fn();
  };

  return () => {
    // Bound the total wait from the FIRST event of a burst: a continuous
    // stream must still refresh periodically instead of postponing forever.
    if (hardTimer === null) {
      hardTimer = window.setTimeout(fire, maxWaitMs);
    }
    if (softTimer !== null) window.clearTimeout(softTimer);
    softTimer = window.setTimeout(fire, delayMs);
    return {
      cancel: () => clear(),
    };
  };
}
