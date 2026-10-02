"use client";

import { useEffect, useRef, useState } from "react";

export type Polled<T> = { data?: T; error?: string; loading: boolean };

/**
 * Read-only polling. `key` names what is being watched: when it changes, results still in flight for the old key
 * are dropped without touching state, so a late answer never lands on a screen that has moved on.
 * `everyMs` null loads once; returning `stop` from `load` ends polling (the watched work finished).
 */
export function usePoll<T>(key: string | null, load: () => Promise<T>, everyMs: number | null,
                           done?: (data: T) => boolean): Polled<T> & { reload: () => void } {
  const [state, setState] = useState<Polled<T> & { key: string | null }>({ loading: !!key, key });
  const [nonce, setNonce] = useState(0);
  const loadRef = useRef(load);
  const doneRef = useRef(done);
  useEffect(() => {
    loadRef.current = load;
    doneRef.current = done;
  });

  useEffect(() => {
    if (!key) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      let finished = false;
      try {
        const data = await loadRef.current();
        if (!alive) return;
        finished = !!doneRef.current?.(data);
        setState({ data, loading: false, key });
      } catch (e) {
        if (!alive) return;
        setState((s) => ({ ...s, error: e instanceof Error ? e.message : String(e), loading: false, key }));
      }
      if (alive && everyMs && !finished) timer = setTimeout(tick, everyMs);
    };
    tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [key, everyMs, nonce]);

  const current = state.key === key ? state : { loading: !!key };
  return { ...current, reload: () => setNonce((n) => n + 1) };
}

/** Unwraps an openapi-fetch result, turning a refusal into an Error carrying the service's message. */
export async function must<T>(call: Promise<{ data?: T; error?: unknown }>, fallback: (e: unknown) => string): Promise<T> {
  const { data, error } = await call;
  if (error !== undefined || data === undefined) throw new Error(fallback(error));
  return data;
}
