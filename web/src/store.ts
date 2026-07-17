import { create } from "zustand";

export interface ModuleConfig {
  id: string;
  title: string;
  kind: "iframe" | "panel";
  url?: string;
  panel?: string;
}

export interface AppConfig {
  machine: string;
  modules: ModuleConfig[];
}

interface State {
  config: AppConfig | null;
  activeModuleId: string | null;
  setConfig: (c: AppConfig) => void;
  setActive: (id: string) => void;
}

export const useStore = create<State>((set) => ({
  config: null,
  activeModuleId: null,
  setConfig: (config) => set({ config }),
  setActive: (activeModuleId) => set({ activeModuleId }),
}));
