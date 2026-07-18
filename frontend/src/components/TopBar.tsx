import { useState, type CSSProperties } from "react";
import { fetchJSON, usePolling } from "../api";
import { useStore } from "../store";

/**
 * Cockpit catalog dropdowns + config buttons. Each only *types* a short string
 * into the tmux session (POST /api/terminal/insert) — NO Enter — then flips the
 * active module to TERMINAL so the typed text is visible. Naz reviews and
 * presses Enter in the pane. (Telemetry + clock moved to the bottom of
 * ModuleRail.)
 */

interface CatalogEntry {
  name: string;
  insert: string;
  group: string;
}
interface Catalog {
  skills: CatalogEntry[];
  marketplace_skills: CatalogEntry[];
  mcps: CatalogEntry[];
}

const SELECT_STYLE: CSSProperties = {
  background: "var(--color-panel-2)",
  color: "var(--color-phosphor-dim)",
  border: "1px solid var(--color-edge)",
  padding: "4px 6px",
};

/** Group entries preserving first-appearance order (OTHER sinks to its place). */
function grouped(items: CatalogEntry[]): { name: string; items: CatalogEntry[] }[] {
  const order: string[] = [];
  const map = new Map<string, CatalogEntry[]>();
  for (const it of items) {
    if (!map.has(it.group)) {
      map.set(it.group, []);
      order.push(it.group);
    }
    map.get(it.group)!.push(it);
  }
  return order.map((name) => ({ name, items: map.get(name)! }));
}

function OptGroup({ name, items }: { name: string; items: CatalogEntry[] }) {
  return (
    <optgroup label={name}>
      {items.map((it) => (
        <option key={it.name} value={it.insert}>
          {it.name}
        </option>
      ))}
    </optgroup>
  );
}

export default function TopBar() {
  const machine = useStore((s) => s.config?.machine);
  const buttons = useStore((s) => s.config?.buttons) ?? [];
  const [skillSel, setSkillSel] = useState("");
  const [mktSel, setMktSel] = useState("");
  const [mcpSel, setMcpSel] = useState("");

  const { data: catalog } = usePolling<Catalog>("/api/catalog", 60_000);

  const insert = async (text: string) => {
    try {
      await fetchJSON("/api/terminal/insert", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const tmux = useStore.getState().config?.modules.find((m) => m.id === "tmux");
      if (tmux) useStore.getState().setActive(tmux.id);
    } catch (e) {
      console.error("terminal insert failed", e);
    }
  };

  const skills = grouped(catalog?.skills ?? []);
  const mktSkills = grouped(catalog?.marketplace_skills ?? []);
  const mcps = grouped(catalog?.mcps ?? []);

  return (
    <header className="hud hud--bracket reveal reveal-1 m-2 mb-0 flex flex-wrap items-center justify-between gap-2 px-4 py-2">
      <div className="flex items-center gap-3">
        <span className="panel-title">▸ RAILJACK</span>
        <span className="label">// {(machine ?? "—").toUpperCase()}</span>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select
          className="mono label"
          style={SELECT_STYLE}
          value={skillSel}
          onChange={(e) => {
            const v = e.target.value;
            if (v) {
              void insert(v);
              setSkillSel("");
            }
          }}
        >
          <option value="" disabled>
            SKILLS
          </option>
          {skills.map((g) => (
            <OptGroup key={g.name} {...g} />
          ))}
        </select>

        {mktSkills.length > 0 && (
          <select
            className="mono label"
            style={SELECT_STYLE}
            value={mktSel}
            onChange={(e) => {
              const v = e.target.value;
              if (v) {
                void insert(v);
                setMktSel("");
              }
            }}
          >
            <option value="" disabled>
              MARKETPLACE
            </option>
            {mktSkills.map((g) => (
              <OptGroup key={g.name} {...g} />
            ))}
          </select>
        )}

        <select
          className="mono label"
          style={SELECT_STYLE}
          value={mcpSel}
          onChange={(e) => {
            const v = e.target.value;
            if (v) {
              void insert(v);
              setMcpSel("");
            }
          }}
        >
          <option value="" disabled>
            MCP
          </option>
          {mcps.map((g) => (
            <OptGroup key={g.name} {...g} />
          ))}
        </select>

        {buttons.map((b) => (
          <button key={b.label} className="btn btn--signal" onClick={() => void insert(b.insert)}>
            {b.label}
          </button>
        ))}
      </div>
    </header>
  );
}
