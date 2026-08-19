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

export interface DaySummary {
  date: string;
  reminders: number;
  prompts: number;
  completed: number;
  total: number;
}

export interface MonthOverview {
  year: number;
  month: number;
  month_name: string;
  days: Record<string, DaySummary>;
}

export interface CalendarTask {
  id: string;
  date?: string;
  type: "reminder" | "prompt_task";
  title: string;
  status: "pending" | "in_progress" | "completed" | "snoozed";
  tags?: string[];
  target_repo?: string;
  prompt?: string;
  interpolated_prompt?: string;
  cron?: string;
  is_recurring?: boolean;
}

export interface DayTasksResponse {
  date: string;
  date_formatted: string;
  tasks: CalendarTask[];
}

export interface CalendarSyncResult {
  status: "ok" | "local-only" | "conflict" | "error" | "no-repo";
  committed?: boolean;
  pulled?: boolean;
  pushed?: boolean;
  detail?: string;
}

export async function fetchMonthOverview(year?: number, month?: number): Promise<MonthOverview> {
  const params = new URLSearchParams();
  if (year) params.append("year", String(year));
  if (month) params.append("month", String(month));
  const query = params.toString() ? `?${params.toString()}` : "";
  return fetchJSON<MonthOverview>(`/api/calendar/month${query}`);
}

export async function fetchDayTasks(date?: string): Promise<DayTasksResponse> {
  const query = date ? `?date=${date}` : "";
  return fetchJSON<DayTasksResponse>(`/api/calendar/day${query}`);
}

export async function createCalendarTask(body: {
  date?: string;
  type: "reminder" | "prompt_task";
  title: string;
  status?: string;
  tags?: string[];
  target_repo?: string;
  prompt?: string;
  cron?: string;
}): Promise<{ status: string; task: CalendarTask; sync?: CalendarSyncResult }> {
  return fetchJSON("/api/calendar/task", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function updateCalendarTaskStatus(
  taskId: string,
  status: "pending" | "in_progress" | "completed" | "snoozed"
): Promise<{ status: string; id: string; new_status: string; sync?: CalendarSyncResult }> {
  return fetchJSON(`/api/calendar/task/${taskId}/status`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
}

export async function deleteCalendarTask(taskId: string): Promise<{ status: string; deleted_id: string; sync?: CalendarSyncResult }> {
  return fetchJSON(`/api/calendar/task/${taskId}`, {
    method: "DELETE",
  });
}

export function syncCalendar(): Promise<CalendarSyncResult> {
  return fetchJSON("/api/calendar/sync", { method: "POST" });
}

export async function dispatchCalendarPrompt(
  taskId: string,
  date?: string,
  customPrompt?: string
): Promise<{ status: string; detail?: string }> {
  return fetchJSON(`/api/calendar/task/${taskId}/dispatch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ date, custom_prompt: customPrompt }),
  });
}

