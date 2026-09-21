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

/** One provider telemetry lane from /api/session — the JEV slice only. */
interface JevLane {
  remaining_usd?: number;
  refill_usd?: number;
  reset_at?: string;
  calls?: number;
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

/** JEV button v2 — click copies a ready-to-paste metered ask-Jev command for
 * the selected primitive: noul (yes/no), choice (pick 1 of N), score (0–10
 * rubric). `--id` is required for choice/score (askjev.py rejects without).
 * --min-confidence deliberately NOT in the copied text — the default 0.6 gate
 * is what the user should see; rc=3 is a valid "gate says no" answer.
 * Segmented mode chips remember their setting in localStorage. Label shows the
 * live house balance from /api/session → lanes.jev (18th→18th UTC window).
 * Slot: between ✍ HANDOFF and 🧠 LESSON (Naz, 2026-09-21; metered Jev Choice
 * 1.0). v2 spec: Damriw workorder 2026-09-21 night, Naz-approved. */
type JevMode = "noul" | "choice" | "score";

const JEV_CMDS: Record<JevMode, string> = {
  noul:
    "python3 ~/.hermes/scripts/jev_meter.py run noul --state '<goal + context + constraints, no secrets>' --question '<yes/no question>'",
  choice:
    "python3 ~/.hermes/scripts/jev_meter.py run choice --state '<goal + context + constraints, no secrets>' --question '<which-option question>' --option '<option A>' --option '<option B>' --option '<option C>' --id '<short-label>'",
  score:
    "python3 ~/.hermes/scripts/jev_meter.py run score --state '<goal + context + constraints, no secrets>' --question '<how-strong question>' --level '0 <lowest meaning>' --level '5 <middle meaning>' --level '10 <highest meaning>' --id '<short-label>'",
};

const JEV_MODE_KEY = "jev-mode";

function JevButton() {
  const { data } = usePolling<{ lanes?: { jev?: JevLane } }>("/api/session", 15_000);
  const [copied, setCopied] = useState(false);
  // Remembered across sessions like the theme toggle; localStorage is
  // browser-only so guard the initial read.
  const [mode, setMode] = useState<JevMode>(() => {
    const saved = typeof document !== "undefined" ? localStorage.getItem(JEV_MODE_KEY) : null;
    return saved === "choice" || saved === "score" ? saved : "noul";
  });
  const j = data?.lanes?.jev;
  const pick = (m: JevMode) => {
    setMode(m);
    localStorage.setItem(JEV_MODE_KEY, m);
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(JEV_CMDS[mode]);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (e) {
      console.error("clipboard copy failed", e);
    }
  };
  return (
    <span className="flex items-center gap-1">
      <span className="flex">
        {(["noul", "choice", "score"] as JevMode[]).map((m) => (
          <button
            key={m}
            className="btn btn--compact label"
            style={{
              fontSize: "9px",
              padding: "2px 4px",
              background: mode === m ? "var(--color-panel-2)" : undefined,
              color: mode === m ? "var(--color-phosphor)" : "var(--color-phosphor-dim)",
            }}
            onClick={() => pick(m)}
            title={`${m} — copy this primitive's metered command template`}
          >
            {m.toUpperCase()}
          </button>
        ))}
      </span>
      <button
        className="btn btn--compact"
        onClick={() => void copy()}
        title={`Copy the metered ${mode} command — fill the <…> fields, paste, Enter. Label = live house JEV balance.`}
      >
        {copied ? "✓ COPIED!" : j?.remaining_usd != null ? `◈ JEV $${j.remaining_usd.toFixed(2)}` : "◈ JEV …"}
      </button>
    </span>
  );
}

export default function CockpitControls() {
  const buttons = useStore((s) => s.config?.buttons) ?? [];
  const [skillSel, setSkillSel] = useState("");
  const [mktSel, setMktSel] = useState("");
  const [mcpSel, setMcpSel] = useState("");
  const [restarting, setRestarting] = useState(false);
  const [handoffTo, setHandoffTo] = useState("Tasai");
  const [copiedLabel, setCopiedLabel] = useState<string | null>(null);
  // Mandatory-question state: label of the config button currently asking its
  // inline YES/NO question (buttons with `ask:` in the machine YAML). A plain
  // click on such a button NEVER copies — it only opens the question; YES/NO
  // copy and close, clicking the button again cancels without copying.
  const [askOpen, setAskOpen] = useState<string | null>(null);

  // Theme toggle — flips data-theme on <html> and persists to localStorage.
  // index.html sets it pre-paint from localStorage so a reload is flash-free;
  // light tokens live in index.css under [data-theme="light"].
  const [light, setLight] = useState(
    () => typeof document !== "undefined" && document.documentElement.dataset.theme === "light",
  );
  const toggleTheme = () => {
    const next = !light;
    const val = next ? "light" : "dark";
    document.documentElement.dataset.theme = val;
    localStorage.setItem("theme", val);
    setLight(next);
  };

  const { data: catalog, refetch: refetchCatalog } = usePolling<Catalog>("/api/catalog", 60_000);

  const copyToClipboard = async (text: string, label?: string): Promise<boolean> => {
    try {
      await navigator.clipboard.writeText(text);
      if (label) {
        setCopiedLabel(label);
        setTimeout(() => setCopiedLabel(null), 1500);
      }
      return true;
    } catch (e) {
      console.error("clipboard copy failed", e);
      return false;
    }
  };

  // Re-read the machine YAML on the server (buttons/modules) AND rescan the
  // catalog (skills / marketplace / MCP) — no F5, no uvicorn restart.
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
          location.reload();
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
If it is ambiguous what feature just finished, ask me before writing. The recipient is ${recipient}.`;

  // Learning layer (see ~/Cephalon/10-knowledge/practices.md): one prompt, the
  // agent files the lesson into the vault staging areas. Solution-vs-practice
  // is the agent's judgment, stated in the entry.
  const lessonMeta = `File a lesson from this session into the Cephalon vault (~/Cephalon). First decide its TYPE:
- PRACTICE (how we work — a repeatable rule for building/operating): append ONE line to 00-raw_ideas/practices-pending.md matching the entry format shown at the top of that file (date | one-line rule | scope | evidence).
- SOLUTION (what we know — reusable knowledge worth a note): create a draft note in 00-raw_ideas/knowledge-drafts/ (kebab-case filename, YAML frontmatter with title/date/tags/category, one idea per note) and list it in 00-raw_ideas/raw_ideas-index.md under the staging Subdirectory entry.
Rules: evidence-backed (cite what happened this session), one idea per entry, redact secrets and personal data, never restate something already in naz-profile.md § Working Agreements or an existing vault note — search first, link instead. Do not modify 10-knowledge/practices.md or promote anything — promotion happens at the next vault session start. If no genuine lesson emerged this session, say so and write nothing.`;

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
            void copyToClipboard(v, "SKILL");
            setSkillSel("");
          }
        }}
      >
        <option value="" disabled>
          {copiedLabel === "SKILL" ? "✓ COPIED!" : "SKILLS"}
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
              void copyToClipboard(v, "MARKETPLACE");
              setMktSel("");
            }
          }}
        >
          <option value="" disabled>
            {copiedLabel === "MARKETPLACE" ? "✓ COPIED!" : "MARKETPLACE"}
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
            void copyToClipboard(v, "MCP");
            setMcpSel("");
          }
        }}
      >
        <option value="" disabled>
          {copiedLabel === "MCP" ? "✓ COPIED!" : "MCP"}
        </option>
        {mcps.map((g) => (
          <OptGroup key={g.name} {...g} />
        ))}
      </select>

      {buttons.map((b) =>
        b.ask && askOpen === b.label ? (
          // Inline mandatory question (no native confirm() — it silently
          // no-ops in embedded contexts). YES = insert + append_yes,
          // NO = plain insert; both collapse back to the button.
          <span key={b.label} className="flex items-center gap-1.5">
            <span className="mono label" style={SELECT_STYLE}>
              {b.ask}
            </span>
            <button
              className="btn btn--compact btn--signal"
              title="Copy the prompt WITH the extra step"
              onClick={() => {
                void copyToClipboard(b.append_yes ? `${b.insert} ${b.append_yes}` : b.insert, b.label);
                setAskOpen(null);
              }}
            >
              YES
            </button>
            <button
              className="btn btn--compact"
              title="Copy the prompt as-is"
              onClick={() => {
                void copyToClipboard(b.insert, b.label);
                setAskOpen(null);
              }}
            >
              NO
            </button>
          </span>
        ) : (
          <button
            key={b.label}
            className="btn btn--signal btn--compact"
            title={b.ask ? `${b.label} — asks one question before copying` : undefined}
            onClick={() => {
              if (b.ask) {
                setAskOpen(askOpen === b.label ? null : b.label);
                return;
              }
              void copyToClipboard(b.insert, b.label);
            }}
          >
            {copiedLabel === b.label ? "✓ COPIED!" : b.label}
          </button>
        ),
      )}

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
        onClick={() => {
          void copyToClipboard(handoffMeta(handoffTo), "HANDOFF");
        }}
        title="Copy prompt to clipboard for writing a port/implementation handoff to the selected sister in readme-naz.md + hot.md"
      >
        {copiedLabel === "HANDOFF" ? "✓ COPIED!" : "✍ HANDOFF"}
      </button>

      <JevButton />

      <button
        className="btn btn--compact"
        onClick={() => {
          void copyToClipboard(lessonMeta, "LESSON");
        }}
        title="Copy prompt to clipboard for filing a lesson (solution or practice) into the Cephalon learning-layer staging areas"
      >
        {copiedLabel === "LESSON" ? "✓ COPIED!" : "🧠 LESSON"}
      </button>

      <button
        className="btn btn--compact"
        onClick={toggleTheme}
        title={light ? "Currently light — switch to dark" : "Currently dark — switch to light"}
      >
        {light ? "☾" : "☀"}
      </button>
    </div>
  );
}
