import { create } from "zustand";

export interface ModuleConfig {
  id: string;
  title: string;
  kind: "iframe" | "panel";
  url?: string;
  panel?: string;
  health?: boolean;
  manage?: boolean;
  start_timeout_s?: number;
}

export interface CockpitButton {
  label: string;
  insert: string;
}

export interface AppConfig {
  machine: string;
  modules: ModuleConfig[];
  buttons?: CockpitButton[];
}

interface State {
  config: AppConfig | null;
  activeModuleId: string | null;
  healthMap: Record<string, string>;
  /** moduleId → epoch-ms deadline after which the amber "STARTING…" state expires. */
  starting: Record<string, number>;
  setConfig: (c: AppConfig) => void;
  setActive: (id: string) => void;
  setHealth: (map: Record<string, string>) => void;
  beginStarting: (id: string, timeoutS: number) => void;
  clearStarting: (id: string) => void;
}

export const useStore = create<State>((set) => ({
  config: null,
  activeModuleId: null,
  healthMap: {},
  starting: {},
  setConfig: (config) => set({ config }),
  setActive: (activeModuleId) => set({ activeModuleId }),
  setHealth: (healthMap) => set({ healthMap }),
  beginStarting: (id, timeoutS) =>
    set((s) => ({ starting: { ...s.starting, [id]: Date.now() + timeoutS * 1000 } })),
  clearStarting: (id) =>
    set((s) => {
      if (!(id in s.starting)) return s;
      const next = { ...s.starting };
      delete next[id];
      return { starting: next };
    }),
}));

export type PipStatus = "ok" | "starting" | "down" | "unknown";

/** Effective pip state: a healthy module is always OK; otherwise it's amber
 * "STARTING…" until its deadline passes, then falls back to the raw health. */
export function pipStatus(
  health: string | undefined,
  deadline: number | undefined,
  now: number,
): PipStatus {
  if (health === "ok") return "ok";
  if (deadline !== undefined && now < deadline) return "starting";
  return health === "down" ? "down" : "unknown";
}
