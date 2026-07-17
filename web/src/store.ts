import { create } from "zustand";

export interface ModuleConfig {
  id: string;
  title: string;
  kind: "iframe" | "panel";
  url?: string;
  panel?: string;
  health?: boolean;
  manage?: boolean;
}

export interface AppConfig {
  machine: string;
  modules: ModuleConfig[];
}

interface State {
  config: AppConfig | null;
  activeModuleId: string | null;
  healthMap: Record<string, string>;
  setConfig: (c: AppConfig) => void;
  setActive: (id: string) => void;
  setHealth: (map: Record<string, string>) => void;
}

export const useStore = create<State>((set) => ({
  config: null,
  activeModuleId: null,
  healthMap: {},
  setConfig: (config) => set({ config }),
  setActive: (activeModuleId) => set({ activeModuleId }),
  setHealth: (healthMap) => set({ healthMap }),
}));
