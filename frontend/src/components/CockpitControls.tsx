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
  const [restarting, setRestarting] = useState(false);
  const [handoffTo, setHandoffTo] = useState("Tasai");

  const { data: catalog, refetch: refetchCatalog } = usePolling<Catalog>("/api/catalog", 60_000);

  const insert = async (text: string): Promise<boolean> => {
    try {
      await fetchJSON("/api/terminal/insert", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const tmux = useStore.getState().config?.modules.find((m) => m.id === "tmux");
      if (tmux) useStore.getState().setActive(tmux.id);
      return true;
    } catch (e) {
      console.error("terminal insert failed", e);
      return false;
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

  // Restart the railjack server itself (load new backend code after an edit).
  // The endpoint returns fast, then a detached `sleep 1 && systemctl restart`
  // kills these workers ~1 s later — so we expect the connection to drop and
  // poll /api/health until the service is back, then reload the page.
  const restartServer = async () => {
    if (!confirm("Restart railjack now? Loads new backend code — ~2 s downtime.")) return;
    setRestarting(true);
    try {
      await fetchJSON("/api/system/restart", { method: "POST" });
    } catch {
      /* expected — the worker dies ~1 s after responding */
    }
    const deadline = Date.now() + 20_000;
    const tick = setInterval(async () => {
      if (Date.now() > deadline) {
        clearInterval(tick);
        setRestarting(false);
        return;
      }
      try {
        const r = await fetch("/api/health");
        if (r.ok) {
          clearInterval(tick);
          location.reload(); // pick up any frontend rebuild too
        }
      } catch {
        /* still restarting */
      }
    }, 500);
  };

  const handoffMeta = (recipient: string) => `Write a port/implementation handoff for the feature I just finished, addressed to ${recipient}. Do all of this:
1. Append a full, self-contained replicate/implement prompt under the "## Sister handoffs (ack-by-deleting)" section in ~/Cephalon/readme-naz.md. Match the format of the existing entries there exactly: a ### heading, a dash-bracket todo line, a fenced self-contained prompt block, and a ctx: line of wikilinks to the design note.
2. Add a one-line pointer under the "Pending sister handoffs" block in ~/Cephalon/hot.md.
3. git pull --ff-only first, preserve frontmatter, commit and push both files, and append a one-line entry to ~/Cephalon/logs/memory-log.md.
If it is ambiguous what feature just finished, ask me before writing. The recipient is ${recipient}.`.replace(/\s+/g, " ").trim();

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

      <button
        className="btn btn--compact"
        onClick={() => void restartServer()}
        disabled={restarting}
        title="Restart the railjack server itself — loads new backend code after Antigravity/Tawhan edit the app. ~2 s downtime."
      >
        {restarting ? "↻ SVC…" : "↻ SVC"}
      </button>

      <select
        className="mono label"
        style={SELECT_STYLE}
        value={handoffTo}
        onChange={(e) => setHandoffTo(e.target.value)}
      >
        <option value="Tasai">Tasai</option>
        <option value="Tawhan">Tawhan</option>
      </select>

      <button
        className="btn btn--compact"
        onClick={async () => {
          if (!(await insert(handoffMeta(handoffTo))))
            alert("✍ HANDOFF failed to type into the terminal — is the tmux pane open? (see browser console)");
        }}
        title="Type a prompt into the terminal that tells the agent to write a port/implementation handoff to the selected sister and file it in readme-naz.md + hot.md"
      >
        ✍ HANDOFF
      </button>
    </div>
  );
}
