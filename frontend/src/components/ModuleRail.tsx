import { useStore, pipStatus } from "../store";

export default function ModuleRail() {
  const modules = useStore((s) => s.config?.modules) ?? [];
  const activeId = useStore((s) => s.activeModuleId);
  const setActive = useStore((s) => s.setActive);
  const healthMap = useStore((s) => s.healthMap);
  const starting = useStore((s) => s.starting);

  return (
    <nav className="hud hud--bracket reveal reveal-2 m-2 flex w-44 shrink-0 flex-col gap-1 p-2">
      <span className="label mb-1 px-1">MODULES</span>
      {modules.map((m) => {
        const active = m.id === activeId;
        const status = pipStatus(healthMap[m.id], starting[m.id], Date.now());
        const pip =
          status === "ok"
            ? "pip pip--go"
            : status === "starting"
              ? "pip pip--hazard"
              : status === "down"
                ? "pip pip--crit"
                : "pip";
        return (
          <button
            key={m.id}
            onClick={() => setActive(m.id)}
            className={`btn hud--bracket relative flex items-center gap-2 ${active ? "btn--signal" : ""}`}
          >
            <span className={pip} aria-hidden />
            <span className="text-left">{m.title}</span>
          </button>
        );
      })}
    </nav>
  );
}
