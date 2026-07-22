import { useCallback, useEffect, useState } from "react";

export async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return (await res.json()) as T;
}

/** Poll a JSON endpoint on an interval. Used by M2 health; config is static so
 * it fetches once and re-checks occasionally. */
export function usePolling<T>(url: string, intervalMs: number): {
  data: T | null;
  error: string | null;
  refetch: () => Promise<void>;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Manual, out-of-band refresh (e.g. after a ↻ CFG reload) — same fetch as the
  // interval tick but on demand, so a caller can pull fresh data immediately
  // instead of waiting out the poll interval.
  const refetch = useCallback(async () => {
    try {
      const d = await fetchJSON<T>(url);
      setData(d);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [url]);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const d = await fetchJSON<T>(url);
        if (alive) {
          setData(d);
          setError(null);
        }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      }
    };
    tick();
    const t = setInterval(tick, intervalMs);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [url, intervalMs]);

  return { data, error, refetch };
}
