import { useStore } from "../store";

export default function ServicesList() {
  const health = useStore((s) => s.health);
  const setEmbed = useStore((s) => s.setEmbed);
  const services = health?.services ?? [];
  const mcp = health?.mcp_servers ?? [];

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
      <div className="mb-4">
        <span className="label !text-[10px] text-faint block mb-2">TILES</span>
        {services.length === 0 ? (
          <p className="text-[11px] text-faint">No services configured. Define ORBITER_SERVICES.</p>
        ) : (
          <div className="grid grid-cols-2 gap-2">
            {services.map((srv) => {
              let host = srv.url;
              try {
                host = new URL(srv.url).hostname;
              } catch {
                // ignore
              }
              return (
                <div
                  key={srv.name}
                  className="hud border border-edge bg-void/30 p-2 flex flex-col justify-between h-[80px]"
                >
                  <div className="min-w-0">
                    <div className="text-[11px] font-semibold text-phosphor-dim truncate">{srv.name}</div>
                    <div className="mono text-[8px] text-faint truncate">{host}</div>
                  </div>
                  <div className="flex gap-2 mt-2 shrink-0">
                    <a
                      href={srv.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="btn !px-1.5 !py-0.5 !text-[9px] text-center"
                    >
                      ↗ TAB
                    </a>
                    {srv.embed && (
                      <button
                        onClick={() => setEmbed(srv)}
                        className="btn !px-1.5 !py-0.5 !text-[9px]"
                      >
                        EMBED
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="border-t border-edge-soft pt-3">
        <span className="label !text-[10px] text-faint block mb-2">MCP SERVERS</span>
        {mcp.length === 0 ? (
          <div className="mono text-[10px] text-faint">NONE LOADED</div>
        ) : (
          mcp.map((s) => (
            <div
              key={s.name}
              className="mono flex items-center gap-2 border-b border-edge-soft py-1.5 text-[10px] last:border-0"
            >
              <span className="text-signal">▸</span>
              <span className="truncate text-phosphor-dim">{s.name}</span>
              <span className="ml-auto uppercase text-faint">{s.type}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
