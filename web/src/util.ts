import type { SessionStatus } from "./types";

export function cn(...xs: (string | false | null | undefined)[]): string {
  return xs.filter(Boolean).join(" ");
}

export interface StatusMeta {
  pip: string;
  label: string;
  color: string;
}

export function statusMeta(s: SessionStatus | undefined): StatusMeta {
  switch (s) {
    case "running":
      return { pip: "pip--signal", label: "EXECUTING", color: "var(--color-signal)" };
    case "pending_admission":
      return { pip: "pip--hazard", label: "AWAITING ADMISSION", color: "var(--color-hazard)" };
    case "waiting_approval":
      return { pip: "pip--hazard", label: "AWAITING APPROVAL", color: "var(--color-hazard)" };
    case "completed":
      return { pip: "pip--go", label: "COMPLETE", color: "var(--color-go)" };
    case "failed":
      return { pip: "pip--crit", label: "FAULT", color: "var(--color-critical)" };
    default:
      return { pip: "", label: "IDLE", color: "var(--color-muted)" };
  }
}

export function connMeta(readyState: number) {
  if (readyState === 1) return { label: "LINK ACTIVE", pip: "pip--signal" };
  if (readyState === 0) return { label: "LINKING", pip: "pip--hazard" };
  return { label: "LINK DOWN", pip: "pip--crit" };
}

export function clock(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}Z`;
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}
