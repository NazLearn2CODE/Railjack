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
  /** Opt-in: show the LIVE terminal dock only while this module is active.
   *  Default (undefined/false) = hidden. Set `live_dock: true` in the machine
   *  YAML for any module that wants the dock (only n8n today). */
  live_dock?: boolean;
}

export interface CockpitButton {
  label: string;
  insert: string;
}

export interface DockConfig {
  title: string;
  url: string;
  height: number;
}

export interface AppConfig {
  machine: string;
  modules: ModuleConfig[];
  buttons?: CockpitButton[];
  dock?: DockConfig;
}

interface State {
  config: AppConfig | null;
  activeModuleId: string | null;
  healthMap: Record<string, string>;
  /** moduleId → epoch-ms deadline after which the amber "STARTING…" state expires. */
  starting: Record<string, number>;
  /** moduleId → whether the LIVE dock is toggled open. Undefined = untouched, so
   *  the module falls back to its `live_dock` YAML default (see dockOpenFor). */
  dockOpen: Record<string, boolean>;
  setConfig: (c: AppConfig) => void;
  setActive: (id: string) => void;
  setHealth: (map: Record<string, string>) => void;
  beginStarting: (id: string, timeoutS: number) => void;
  clearStarting: (id: string) => void;
  /** Flip the LIVE dock open/closed for one module (top-bar LIVE button). */
  toggleDock: (id: string) => void;
}

export const useStore = create<State>((set) => ({
  config: null,
  activeModuleId: null,
  healthMap: {},
  starting: {},
  dockOpen: {},
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
  toggleDock: (id) =>
    set((s) => {
      const mod = s.config?.modules.find((m) => m.id === id);
      // Read the current effective state (toggle override, else YAML default),
      // then store its inverse so the next read flips it.
      const current = id in s.dockOpen ? s.dockOpen[id] : Boolean(mod?.live_dock);
      return { dockOpen: { ...s.dockOpen, [id]: !current } };
    }),
}));

/** Effective LIVE-dock visibility for a module: the user's live toggle if they
 *  have touched the button, otherwise the module's `live_dock` YAML default
 *  (n8n ships open; every other module ships closed). */
export function dockOpenFor(
  dockOpen: Record<string, boolean>,
  module: ModuleConfig | undefined,
): boolean {
  if (!module) return false;
  return module.id in dockOpen ? dockOpen[module.id] : Boolean(module.live_dock);
}

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
