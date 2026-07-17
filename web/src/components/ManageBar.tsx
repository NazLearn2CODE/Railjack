import { useState } from "react";
import { fetchJSON } from "../api";
import { useStore, pipStatus } from "../store";

/**
 * START / STOP / RESTART + LOGS drawer for a manageable module. Rendered above
 * the iframe frame area (never inside the iframe map) so toggling it can't
 * remount the tmux terminal. After any action we refetch /api/health so the pip
 * updates immediately instead of waiting for the next 5 s tick.
 */
export default function ManageBar({ moduleId }: { moduleId: string }) {
  const health = useStore((s) => s.healthMap[moduleId]);
  const deadline = useStore((s) => s.starting[moduleId]);
  const timeoutS = useStore(
    (s) => s.config?.modules.find((m) => m.id === moduleId)?.start_timeout_s ?? 150,
  );
  const [busy, setBusy] = useState(false);
  const [logsOpen, setLogsOpen] = useState(false);
  const [logs, setLogs] = useState("");

  const refetchHealth = async () => {
    try {
      const h = await fetchJSON<Record<string, string>>("/api/health");
      useStore.getState().setHealth(h);
    } catch {
      /* next poll tick recovers */
    }
  };

  const act = async (action: "start" | "stop" | "restart") => {
    setBusy(true);
    try {
      const r = await fetchJSON<{ status: string; detail: string }>(
        `/api/modules/${moduleId}/action`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ action }),
        },
      );
      // A fire-and-forget (slow) start returns "pending": hold the pip amber
      // "STARTING…" until health flips green or start_timeout_s elapses.
      if (r.status === "pending") {
        useStore.getState().beginStarting(moduleId, timeoutS);
      } else if (action === "stop") {
        useStore.getState().clearStarting(moduleId);
      }
      if (r.status === "error") console.error(`${action} failed`, r.detail);
    } catch (e) {
      console.error(`${action} failed`, e);
    } finally {
      setBusy(false);
      await refetchHealth();
    }
  };

  const toggleLogs = async () => {
    if (logsOpen) {
      setLogsOpen(false);
      return;
    }
    try {
      const r = await fetchJSON<{ lines: string }>(
        `/api/modules/${moduleId}/logs?n=50`,
      );
      setLogs(r.lines || "(no output)");
    } catch (e) {
      setLogs(String(e));
    }
    setLogsOpen(true);
  };

  const status = pipStatus(health, deadline, Date.now());
  const healthText =
    status === "ok"
      ? "OK"
      : status === "starting"
        ? "STARTING…"
        : status === "down"
          ? "DOWN"
          : "UNKNOWN";

  return (
    <div className="hud hud--bracket m-2 mb-0 flex flex-col gap-2 p-2">
      <div className="flex items-center gap-2">
        <span className="label">HEALTH</span>
        <span className="mono">{healthText}</span>
        <span className="flex-1" />
        <button className="btn btn--signal" disabled={busy} onClick={() => act("start")}>
          START
        </button>
        <button className="btn btn--crit" disabled={busy} onClick={() => act("stop")}>
          STOP
        </button>
        <button className="btn" disabled={busy} onClick={() => act("restart")}>
          RESTART
        </button>
        <button className="btn" disabled={busy} onClick={toggleLogs}>
          {logsOpen ? "HIDE LOGS" : "LOGS"}
        </button>
      </div>
      {logsOpen && (
        <pre className="mono max-h-48 overflow-auto whitespace-pre-wrap border border-edge bg-void p-2 text-xs">
          {logs}
        </pre>
      )}
    </div>
  );
}
