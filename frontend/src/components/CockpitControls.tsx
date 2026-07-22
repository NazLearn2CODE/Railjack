import { useState, type CSSProperties } from "react";
import { fetchJSON, usePolling } from "../api";
import { useStore, type AppConfig } from "../store";

/**
 * Cockpit catalog dropdowns + config buttons. Each only *types* a short string
 * into the tmux session (POST /api/terminal/insert) — NO Enter — then flips the
 * active module to TERMINAL so the typed text is visible. Naz reviews and
 * presses Enter in the pane. Sits in the FramePanel header row.
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
  padding: "3px 5px",
  fontSize: "11px",
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

export default function CockpitControls() {
  const buttons = useStore((s) => s.config?.buttons) ?? [];
  const [skillSel, setSkillSel] = useState("");
  const [mktSel, setMktSel] = useState("");
  const [mcpSel, setMcpSel] = useState("");

  const { data: catalog, refetch: refetchCatalog } = usePolling<Catalog>("/api/catalog", 60_000);

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

  // Re-read the machine YAML on the server (buttons/modules) AND rescan the
  // catalog (skills / marketplace / MCP) — no F5, no uvicorn restart. Unlike the
  // buttons above, this one does NOT type into the terminal; it just reloads.
  // The catalog reload busts the server-side 60 s cache so a skill/plugin/MCP
  // added since startup appears in the dropdowns right away.
  const [reloading, setReloading] = useState(false);
  const reloadConfig = async () => {
    setReloading(true);
    try {
      const fresh = await fetchJSON<AppConfig>("/api/config/reload", { method: "POST" });
      useStore.getState().setConfig(fresh);
      await fetchJSON<Catalog>("/api/catalog/reload", { method: "POST" });
      await refetchCatalog();
    } catch (e) {
      console.error("config reload failed", e);
    } finally {
      setReloading(false);
    }
  };

  const skills = grouped(catalog?.skills ?? []);
  const mktSkills = grouped(catalog?.marketplace_skills ?? []);
  const mcps = grouped(catalog?.mcps ?? []);

  return (
    <div className="flex flex-wrap items-center gap-1.5">
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
        <button key={b.label} className="btn btn--signal btn--compact" onClick={() => void insert(b.insert)}>
          {b.label}
        </button>
      ))}

      <button
        className="btn btn--compact"
        onClick={() => void reloadConfig()}
        disabled={reloading}
        title="Re-read the machine YAML (buttons/modules) and rescan skills / marketplace / MCP — applies edits and surfaces newly added skills without restarting the server"
      >
        {reloading ? "↻…" : "↻ CFG"}
      </button>
    </div>
  );
}
