import { useCallback, useEffect, useRef, useState } from "react";
import { marked } from "marked";
import { fetchJSON, usePolling } from "../api";
import { useStore, type ModuleConfig } from "../store";

/**
 * Research Deck — Google NotebookLM panel. Two-column workbench: left rail
 * (notebook list + new/delete) + right column of hud cards (sources, chat,
 * generate, artifacts, output feed, jobs). Mirrors ComfyPanel/FfmpegPanel
 * idioms: usePolling/fetchJSON, Pip + progress-bar job rows, reveal stagger.
 *
 * Gating: st.available===false → notice + INITIALIZE / RETRY (POST
 * /api/nblm/probe re-resolves the CLI + forces a fresh auth check, flipping
 * the panel live); st.authed===false → login (inserts `notebooklm login` into
 * the terminal then jumps to the tmux module, same module-switch ComfyPanel
 * uses) + RE-CHECK.
 */

interface NblmState {
  available: boolean;
  reason: string | null;
  authed: boolean;
  account: string | null;
}
interface Notebook { id: string; title: string }
interface Source { source_id: string; title: string; status: string }
interface CatalogGroup { flag: string; label: string; values: string[] }
interface CatalogType {
  id: string; label: string; ext: string;
  groups: CatalogGroup[]; needs_instructions: boolean;
}
// ponytail: artifacts are an opaque passthrough — render defensively.
type Artifact = Record<string, unknown>;
interface OutputFile { name: string; path: string; mtime: number; ext: string }
interface Job {
  id: string; kind: string; label: string; status: string; progress: number;
  output_paths: string[] | null; error: string | null; logs: string[];
}
interface Ref { source_id: string; citation_number: number; cited_text: string }
interface Exchange { q: string; answer: string; references: Ref[] }

const CT: Record<string, string> = { "content-type": "application/json" };
const fileUrl = (p: string) => `/api/nblm/outputs/file?path=${encodeURIComponent(p)}`;
const ls = (k: string) => localStorage.getItem(k);
const slugify = (t: string) =>
  t.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
// mtime may arrive as seconds or ms — normalise.
const dateStr = (m: number) => new Date(m < 1e12 ? m * 1000 : m).toLocaleString();
const md = (s: string) => marked.parse(s) as string;

function Pip({ status }: { status: string }) {
  const cls =
    status === "done" ? "pip pip--go" :
    status === "error" ? "pip pip--crit" :
    status === "running" ? "pip pip--signal" :
    status === "cancelled" ? "pip pip--hazard" : "pip";
  return <span className={cls} />;
}
function SourcePip({ status }: { status: string }) {
  const s = status.toLowerCase();
  const cls =
    s === "ready" || s === "done" ? "pip pip--go" :
    s.includes("process") || s === "running" || s === "queued" ? "pip pip--hazard" : "pip";
  return <span className={cls} />;
}

export default function NotebookPanel({ module: _module }: { module: ModuleConfig }) {
  // Left-rail state.
  const [notebooks, setNotebooks] = useState<Notebook[]>([]);
  const [notebookId, setNotebookId] = useState<string>(() => ls("nblm.notebook") ?? "");
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteInput, setDeleteInput] = useState("");

  // Workbench state.
  const [sources, setSources] = useState<Source[]>([]);
  const [catalog, setCatalog] = useState<CatalogType[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [addUrlVal, setAddUrlVal] = useState("");
  const [addPathVal, setAddPathVal] = useState("");
  const [researchQuery, setResearchQuery] = useState("");
  const [researchMode, setResearchMode] = useState<"fast" | "deep">("fast");

  // Chat.
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [question, setQuestion] = useState("");
  const [convId, setConvId] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [polishing, setPolishing] = useState(false);

  // Generate.
  const [genType, setGenType] = useState("");
  const [genOptions, setGenOptions] = useState<Record<string, string>>({});
  const [genInstructions, setGenInstructions] = useState("");
  const [genErr, setGenErr] = useState<string | null>(null);
  const [polishingGen, setPolishingGen] = useState(false);

  // Output feed md expand/collapse.
  const [mdOpen, setMdOpen] = useState<Record<string, boolean>>({});
  const [mdCache, setMdCache] = useState<Record<string, string>>({});

  const { data: state } = usePolling<NblmState>("/api/nblm/state", 30000);
  // INITIALIZE / RETRY: POST /api/nblm/probe re-resolves the CLI + forces a
  // fresh auth check server-side. Its result overrides the polled state until
  // the next poll lands (both hit the same live computation, so they agree).
  const [probed, setProbed] = useState<NblmState | null>(null);
  const [probing, setProbing] = useState(false);
  useEffect(() => { setProbed(null); }, [state]);
  const st = probed ?? state;
  const probe = async () => {
    if (probing) return;
    setProbing(true);
    try {
      const r = await fetchJSON<NblmState>("/api/nblm/probe", { method: "POST" });
      setProbed(r);
    } catch { /* keep current card; the 30s poll re-checks anyway */ }
    finally { setProbing(false); }
  };
  const { data: jobsData } = usePolling<{ jobs: Job[] }>("/api/nblm/jobs", 1000);
  const jobs = jobsData?.jobs ?? [];

  const sel = notebooks.find((n) => n.id === notebookId) ?? null;
  const selId = sel?.id ?? null;
  const outSlug = sel ? slugify(sel.title) : "";
  const { data: outData } = usePolling<{ files: OutputFile[] }>(
    `/api/nblm/outputs?notebook=${encodeURIComponent(outSlug)}`,
    10000,
  );
  const outputs = [...(outData?.files ?? [])].sort((a, b) => b.mtime - a.mtime);

  const refreshNotebooks = useCallback(async (refresh = false) => {
    try {
      const r = await fetchJSON<{ notebooks: Notebook[] }>(
        `/api/nblm/notebooks?refresh=${refresh}`,
      );
      setNotebooks(r.notebooks ?? []);
      return r.notebooks ?? [];
    } catch {
      return [];
    }
  }, []);

  const loadSources = useCallback((id: string) => {
    fetchJSON<{ sources: Source[] }>(`/api/nblm/notebooks/${id}/sources`)
      .then((r) => setSources(r.sources ?? [])).catch(() => {});
  }, []);
  const loadArtifacts = useCallback((id: string) => {
    fetchJSON<{ artifacts: Artifact[] }>(`/api/nblm/notebooks/${id}/artifacts`)
      .then((r) => setArtifacts(r.artifacts ?? [])).catch(() => {});
  }, []);

  const selectNotebook = (id: string) => {
    setNotebookId(id);
    if (id) localStorage.setItem("nblm.notebook", id);
    else localStorage.removeItem("nblm.notebook");
  };

  // Load the notebook list once the service is available + authed (polled OR
  // freshly probed via INITIALIZE — st covers both).
  useEffect(() => {
    if (st?.available && st.authed) refreshNotebooks();
  }, [st?.available, st?.authed, refreshNotebooks]);

  // Catalog is static — one shot.
  useEffect(() => {
    fetchJSON<{ types: CatalogType[] }>("/api/nblm/catalog")
      .then((r) => setCatalog(r.types ?? [])).catch(() => setCatalog([]));
  }, []);

  // When the selected type changes, preselect the first value of each group.
  const genTypeObj = catalog.find((c) => c.id === genType);
  useEffect(() => {
    if (!genTypeObj) { setGenOptions({}); return; }
    const opts: Record<string, string> = {};
    for (const g of genTypeObj.groups) opts[g.flag] = g.values[0] ?? "";
    setGenOptions(opts);
  }, [genTypeObj]);

  // On notebook switch: fetch sources + artifacts, reset chat.
  useEffect(() => {
    if (!selId) { setSources([]); setArtifacts([]); return; }
    loadSources(selId);
    loadArtifacts(selId);
    setConvId(null);
    setExchanges([]);
    setMdOpen({});
    setMdCache({});
  }, [selId, loadSources, loadArtifacts]);

  // Watch jobs: refetch sources/artifacts when a relevant job flips to done.
  const prevDone = useRef<Set<string>>(new Set());
  useEffect(() => {
    const doneIds = new Set(jobs.filter((j) => j.status === "done").map((j) => j.id));
    const newly = jobs.filter((j) => doneIds.has(j.id) && !prevDone.current.has(j.id));
    prevDone.current = doneIds;
    if (!selId) return;
    if (newly.some((j) => j.kind === "source" || j.kind === "research")) loadSources(selId);
    if (newly.some((j) => j.kind === "generate")) loadArtifacts(selId);
  }, [jobs, selId, loadSources, loadArtifacts]);

  // --- mutations -----------------------------------------------------------

  const createNotebook = async () => {
    const t = newTitle.trim();
    if (!t) return;
    try {
      await fetchJSON("/api/nblm/notebooks", {
        method: "POST", headers: CT, body: JSON.stringify({ title: t }),
      });
      setNewTitle("");
      setCreating(false);
      const list = await refreshNotebooks();
      const made = list.find((n) => n.title === t);
      if (made) selectNotebook(made.id);
    } catch { /* keep inline form open */ }
  };

  const deleteNotebook = async () => {
    if (!sel || deleteInput !== sel.title) return;
    try {
      await fetchJSON(`/api/nblm/notebooks/${sel.id}/delete`, {
        method: "POST", headers: CT, body: JSON.stringify({ confirm_title: deleteInput }),
      });
      setDeleteInput("");
      setConfirmDelete(false);
      selectNotebook("");
      await refreshNotebooks();
    } catch { /* 400 title-mismatch can't happen — client gates it */ }
  };

  const addSource = async (body: Record<string, string>, clear: () => void) => {
    if (!selId) return;
    try {
      await fetchJSON(`/api/nblm/notebooks/${selId}/sources`, {
        method: "POST", headers: CT, body: JSON.stringify(body),
      });
      clear();
    } catch { /* jobs card surfaces failures */ }
  };

  const runResearch = async () => {
    if (!selId || !researchQuery.trim()) return;
    try {
      await fetchJSON(`/api/nblm/notebooks/${selId}/research`, {
        method: "POST", headers: CT,
        body: JSON.stringify({ query: researchQuery.trim(), mode: researchMode }),
      });
      setResearchQuery("");
    } catch { /* jobs card surfaces failures */ }
  };

  const polish = async (
    draft: string, purpose: "chat" | "generate", set: (v: string) => void, busy: (b: boolean) => void,
  ) => {
    if (!draft.trim()) return;
    busy(true);
    try {
      const r = await fetchJSON<{ polished: string }>("/api/nblm/polish", {
        method: "POST", headers: CT, body: JSON.stringify({ draft, purpose }),
      });
      set(r.polished);
    } catch { /* non-fatal */ } finally { busy(false); }
  };

  const ask = async () => {
    if (!selId || !question.trim() || asking) return;
    const q = question.trim();
    setQuestion("");
    setAsking(true);
    try {
      const r = await fetchJSON<{
        answer: string; conversation_id: string; references: Ref[];
      }>(`/api/nblm/notebooks/${selId}/ask`, {
        method: "POST", headers: CT,
        body: JSON.stringify({ question: q, conversation_id: convId ?? undefined }),
      });
      setExchanges((prev) => [...prev, { q, answer: r.answer, references: r.references ?? [] }]);
      setConvId(r.conversation_id);
    } catch (e) {
      setExchanges((prev) => [...prev, {
        q, answer: `⚠ ${e instanceof Error ? e.message : String(e)}`, references: [],
      }]);
    } finally { setAsking(false); }
  };

  const generate = async () => {
    if (!selId || !genTypeObj) return;
    setGenErr(null);
    const options: Record<string, string> = {};
    for (const g of genTypeObj.groups) options[g.flag] = genOptions[g.flag] ?? g.values[0] ?? "";
    try {
      await fetchJSON<{ id: string }>(`/api/nblm/notebooks/${selId}/generate`, {
        method: "POST", headers: CT,
        body: JSON.stringify({
          type: genType, options,
          instructions: genInstructions.trim() || undefined,
        }),
      });
    } catch (e) {
      setGenErr(e instanceof Error ? e.message : String(e)); // 409 = one already running
    }
  };

  const downloadArtifact = async (a: Artifact) => {
    if (!selId) return;
    const artifactId = String(a.id ?? a.artifact_id ?? a.key ?? "");
    if (!artifactId) return;
    try {
      await fetchJSON("/api/nblm/artifacts/download", {
        method: "POST", headers: CT,
        body: JSON.stringify({
          notebook_id: selId, artifact_id: artifactId, type: String(a.type ?? ""),
        }),
      });
    } catch { /* jobs card surfaces failures */ }
  };

  const toggleMd = async (f: OutputFile) => {
    const open = !mdOpen[f.path];
    setMdOpen((o) => ({ ...o, [f.path]: open }));
    if (open && !(f.path in mdCache)) {
      try {
        const txt = await (await fetch(fileUrl(f.path))).text();
        setMdCache((c) => ({ ...c, [f.path]: txt }));
      } catch { /* leave empty */ }
    }
  };

  const insertLogin = async () => {
    try {
      await fetchJSON("/api/terminal/insert", {
        method: "POST", headers: CT, body: JSON.stringify({ text: "notebooklm login" }),
      });
      const tmux = useStore.getState().config?.modules.find((m) => m.id === "tmux");
      if (tmux) useStore.getState().setActive(tmux.id);
    } catch { /* non-fatal */ }
  };

  // --- render --------------------------------------------------------------

  if (!st) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <span className="label text-muted">CONNECTING…<span className="caret" /></span>
      </div>
    );
  }
  if (st.available === false) {
    return (
      <div className="flex h-full w-full items-center justify-center p-6">
        <div className="hud hud--bracket reveal reveal-1 flex max-w-md flex-col items-center gap-3 p-6 text-center">
          <span className="panel-title">RESEARCH NOT AVAILABLE ON THIS MACHINE</span>
          <span className="mono text-sm text-muted">{st.reason ?? "see server logs"}</span>
          <button className="btn btn--signal" disabled={probing} onClick={probe}>
            INITIALIZE / RETRY{probing && <span className="caret" />}
          </button>
        </div>
      </div>
    );
  }
  if (st.authed === false) {
    return (
      <div className="flex h-full w-full items-center justify-center p-6">
        <div className="hud hud--bracket reveal reveal-1 flex max-w-md flex-col items-center gap-3 p-6 text-center">
          <span className="panel-title">LOGIN REQUIRED</span>
          <span className="mono text-sm text-muted">
            run <span className="glow-go">notebooklm login</span> in the terminal, then return
          </span>
          {st.reason && <span className="mono text-xs text-muted">{st.reason}</span>}
          <div className="flex gap-1">
            <button className="btn btn--signal" onClick={insertLogin}>
              INSERT LOGIN COMMAND IN TERMINAL
            </button>
            <button className="btn" disabled={probing} onClick={probe}>
              RE-CHECK{probing && <span className="caret" />}
            </button>
          </div>
        </div>
      </div>
    );
  }

  const filtered = notebooks.filter((n) =>
    n.title.toLowerCase().includes(search.toLowerCase()),
  );
  const canGen = !!genTypeObj && (!genTypeObj.needs_instructions || genInstructions.trim().length > 0);

  const imgs = outputs.filter((f) => IMG.has(normExt(f.ext)));

  return (
    <div className="flex h-full w-full gap-2 overflow-hidden p-3">

      {/* LEFT RAIL */}
      <aside className="hud hud--bracket reveal reveal-1 flex w-64 shrink-0 flex-col gap-2 p-3">
        <div className="flex items-center gap-2">
          <span className="panel-title">RESEARCH DECK</span>
          <span className="pip pip--go" />
        </div>
        {st.account && (
          <span className="label truncate text-muted" title={st.account}>{st.account}</span>
        )}
        <input
          className="input" placeholder="search notebooks"
          value={search} onChange={(e) => setSearch(e.target.value)}
        />
        <div className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-auto">
          {filtered.length === 0 && <span className="label px-1 text-muted">no notebooks</span>}
          {filtered.map((n) => (
            <button
              key={n.id}
              onClick={() => selectNotebook(n.id)}
              className={`btn hud--bracket flex items-center gap-2 ${n.id === notebookId ? "btn--signal" : ""}`}
            >
              <span className="truncate text-left">{n.title}</span>
            </button>
          ))}
        </div>

        {!creating ? (
          <button className="btn" onClick={() => setCreating(true)}>+ NEW NOTEBOOK</button>
        ) : (
          <div className="flex flex-col gap-1">
            <input
              className="input" placeholder="notebook title"
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && createNotebook()}
            />
            <div className="flex gap-1">
              <button className="btn btn--signal flex-1" disabled={!newTitle.trim()} onClick={createNotebook}>CREATE</button>
              <button className="btn" onClick={() => { setCreating(false); setNewTitle(""); }}>×</button>
            </div>
          </div>
        )}
        <button className="btn" onClick={() => refreshNotebooks(true)}>↻ REFRESH</button>

        {sel && (
          <div className="mt-auto flex flex-col gap-1 border-t border-edge pt-2">
            {!confirmDelete ? (
              <button className="btn btn--crit" onClick={() => setConfirmDelete(true)}>DELETE NOTEBOOK</button>
            ) : (
              <>
                <input
                  className="input" placeholder="type notebook title to confirm"
                  value={deleteInput} onChange={(e) => setDeleteInput(e.target.value)}
                />
                <div className="flex gap-1">
                  <button
                    className="btn btn--crit flex-1" disabled={deleteInput !== sel.title}
                    onClick={deleteNotebook}
                  >CONFIRM DELETE</button>
                  <button className="btn" onClick={() => { setConfirmDelete(false); setDeleteInput(""); }}>×</button>
                </div>
              </>
            )}
          </div>
        )}
      </aside>

      {/* RIGHT WORKBENCH */}
      <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-auto">
        {!sel ? (
          <div className="hud hud--bracket reveal reveal-2 flex flex-1 items-center justify-center p-6">
            <span className="label text-muted">select a notebook</span>
          </div>
        ) : (
          <>
            {/* SOURCES */}
            <div className="hud hud--bracket reveal reveal-2 flex flex-col gap-2 p-3">
              <span className="label">SOURCES</span>
              {sources.length === 0 && <span className="label text-muted">no sources yet</span>}
              {sources.map((s) => (
                <div key={s.source_id} className="row-in flex items-center gap-2 border border-edge bg-void px-2 py-1">
                  <SourcePip status={s.status} />
                  <span className="mono flex-1 truncate" title={s.title}>{s.title || s.source_id}</span>
                  <span className="label text-muted">{s.status}</span>
                </div>
              ))}
              <div className="flex gap-1">
                <input
                  className="input flex-1" placeholder="https://…"
                  value={addUrlVal} onChange={(e) => setAddUrlVal(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && addSource({ url: addUrlVal.trim() }, () => setAddUrlVal(""))}
                />
                <button className="btn" disabled={!addUrlVal.trim()} onClick={() => addSource({ url: addUrlVal.trim() }, () => setAddUrlVal(""))}>ADD URL</button>
              </div>
              <div className="flex gap-1">
                <input
                  className="input flex-1" placeholder="~/path/to/file.pdf"
                  value={addPathVal} onChange={(e) => setAddPathVal(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && addSource({ path: addPathVal.trim() }, () => setAddPathVal(""))}
                />
                <button className="btn" disabled={!addPathVal.trim()} onClick={() => addSource({ path: addPathVal.trim() }, () => setAddPathVal(""))}>ADD PATH</button>
              </div>
              <div className="flex flex-wrap items-center gap-1">
                <input
                  className="input min-w-40 flex-1" placeholder="research query"
                  value={researchQuery} onChange={(e) => setResearchQuery(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && runResearch()}
                />
                <button className={researchMode === "fast" ? "btn btn--signal" : "btn"} onClick={() => setResearchMode("fast")}>FAST</button>
                <button className={researchMode === "deep" ? "btn btn--signal" : "btn"} onClick={() => setResearchMode("deep")}>DEEP</button>
                <button className="btn btn--signal" disabled={!researchQuery.trim()} onClick={runResearch}>RESEARCH</button>
              </div>
            </div>

            {/* CHAT */}
            <div className="hud hud--bracket reveal reveal-3 flex flex-col gap-2 p-3">
              <div className="flex items-center gap-2">
                <span className="label">CHAT</span>
                <span className="flex-1" />
                <button
                  className="btn" disabled={!convId && exchanges.length === 0}
                  onClick={() => { setConvId(null); setExchanges([]); }}
                >NEW TOPIC</button>
              </div>
              <div className="flex max-h-72 flex-col gap-2 overflow-auto">
                {exchanges.length === 0 && <span className="label text-muted">ask anything about your sources</span>}
                {exchanges.map((ex, i) => (
                  <div key={i} className="flex flex-col gap-1 border border-edge bg-void p-2">
                    <div className="mono text-xs text-muted">Q {ex.q}</div>
                    <div className="prose-md text-sm" dangerouslySetInnerHTML={{ __html: md(ex.answer) }} />
                    {ex.references.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {ex.references.map((r) => (
                          <span
                            key={r.citation_number} className="mono border border-edge px-1 text-xs"
                            title={r.cited_text}
                          >[{r.citation_number}]</span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
              <textarea
                className="input" rows={3} placeholder="ask a question…"
                value={question} onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ask();
                }}
              />
              <div className="flex gap-1">
                <button
                  className="btn" disabled={!question.trim() || polishing}
                  onClick={() => polish(question, "chat", setQuestion, setPolishing)}
                >POLISH{polishing && <span className="caret" />}</button>
                <button
                  className="btn btn--signal flex-1" disabled={!question.trim() || asking}
                  onClick={ask}
                >ASK{asking && <span className="caret" />}</button>
              </div>
            </div>

            {/* GENERATE */}
            <div className="hud hud--bracket reveal reveal-4 flex flex-col gap-2 p-3">
              <span className="label">GENERATE</span>
              <label className="flex items-center gap-2">
                <span className="label whitespace-nowrap">TYPE</span>
                <select className="input flex-1" value={genType} onChange={(e) => setGenType(e.target.value)}>
                  <option value="">—</option>
                  {catalog.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
                </select>
              </label>
              {genTypeObj?.groups.map((g) => (
                <label key={g.flag} className="flex items-center gap-2">
                  <span className="label whitespace-nowrap">{g.label}</span>
                  <select
                    className="input flex-1" value={genOptions[g.flag] ?? ""}
                    onChange={(e) => setGenOptions((o) => ({ ...o, [g.flag]: e.target.value }))}
                  >
                    {g.values.map((v) => <option key={v} value={v}>{v}</option>)}
                  </select>
                </label>
              ))}
              {genType && (
                <textarea
                  className="input" rows={3}
                  placeholder={genTypeObj?.needs_instructions ? "instructions (required)" : "instructions (optional)"}
                  value={genInstructions} onChange={(e) => setGenInstructions(e.target.value)}
                />
              )}
              <span className="label text-muted">GENERATION TAKES 5–45 MIN — WATCH JOBS</span>
              <div className="flex gap-1">
                <button
                  className="btn" disabled={!genInstructions.trim() || polishingGen}
                  onClick={() => polish(genInstructions, "generate", setGenInstructions, setPolishingGen)}
                >POLISH{polishingGen && <span className="caret" />}</button>
                <button className="btn btn--signal flex-1" disabled={!canGen} onClick={generate}>GENERATE</button>
              </div>
              {genErr && <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{genErr}</div>}
            </div>

            {/* ARTIFACTS */}
            <div className="hud hud--bracket reveal reveal-5 flex flex-col gap-2 p-3">
              <span className="label">ARTIFACTS</span>
              {artifacts.length === 0 && <span className="label text-muted">no artifacts yet</span>}
              {artifacts.map((a, i) => <ArtifactRow key={i} a={a} onDownload={() => downloadArtifact(a)} />)}
            </div>

            {/* OUTPUT FEED */}
            <div className="hud hud--bracket reveal reveal-5 flex flex-col gap-2 p-3">
              <span className="label">OUTPUT FEED</span>
              {outputs.length === 0 && <span className="label text-muted">no outputs yet</span>}
              {outputs.map((f) => {
                const ext = normExt(f.ext);
                const url = fileUrl(f.path);
                if (IMG.has(ext)) return null; // rendered in the grid below
                if (MD.has(ext)) {
                  return (
                    <div key={f.path} className="border border-edge bg-void p-2">
                      <button className="btn w-full text-left" onClick={() => toggleMd(f)}>
                        <span className="mono">{mdOpen[f.path] ? "▼" : "▶"} {f.name}</span>
                      </button>
                      {mdOpen[f.path] && (
                        <div className="prose-md mt-2 text-sm" dangerouslySetInnerHTML={{ __html: md(mdCache[f.path] ?? "") }} />
                      )}
                    </div>
                  );
                }
                if (AUDIO.has(ext)) return <audio key={f.path} controls src={url} className="w-full" />;
                if (VIDEO.has(ext)) return <video key={f.path} controls src={url} className="max-h-64 w-full" />;
                // pdf / pptx / csv / json / anything else → download row
                return (
                  <a key={f.path} className="row-in flex items-center gap-2 border border-edge bg-void px-2 py-1"
                    href={url} target="_blank" rel="noreferrer">
                    <span className="mono flex-1 truncate">{f.name}</span>
                    <span className="label text-muted">{dateStr(f.mtime)}</span>
                    <span className="label">⤓</span>
                  </a>
                );
              })}
              {imgs.length > 0 && (
                <div className="grid grid-cols-2 gap-2">
                  {imgs.map((f) => (
                    <a key={f.path} href={fileUrl(f.path)} target="_blank" rel="noreferrer" title={f.name}>
                      <img src={fileUrl(f.path)} alt={f.name} loading="lazy"
                        className="w-full border border-edge object-cover" />
                    </a>
                  ))}
                </div>
              )}
            </div>

            {/* JOBS — copied from ComfyPanel, pointed at /api/nblm */}
            <div className="hud hud--bracket reveal reveal-5 flex flex-col gap-2 p-3">
              <span className="label">JOBS</span>
              {jobs.length === 0 && <span className="label text-muted">no jobs yet</span>}
              {jobs.map((j) => {
                const tail = j.logs.slice(-10);
                const fill =
                  j.status === "done" ? "var(--color-go)" :
                  j.status === "error" ? "var(--color-critical)" :
                  j.status === "cancelled" ? "var(--color-hazard)" :
                  "var(--color-signal)";
                return (
                  <div key={j.id} className="border border-edge bg-void p-2">
                    <div className="flex items-center gap-2">
                      <Pip status={j.status} />
                      <span className="mono">{j.kind}</span>
                      <span className="label truncate flex-1" title={j.label}>{j.label}</span>
                      <span className="label whitespace-nowrap">{j.status.toUpperCase()} · {j.progress}%</span>
                      {(j.status === "running" || j.status === "queued") && (
                        <button
                          className="btn btn--crit"
                          onClick={() => fetchJSON(`/api/nblm/jobs/${j.id}/cancel`, { method: "POST" }).catch(() => {})}
                        >CANCEL</button>
                      )}
                    </div>
                    <div className="mt-1 h-1.5 w-full bg-edge-soft">
                      <div style={{ width: `${j.progress}%`, height: "100%", background: fill }} />
                    </div>
                    {j.error && <div className="mono mt-1 text-xs" style={{ color: "var(--color-critical)" }}>{j.error}</div>}
                    {tail.length > 0 && (
                      <pre className="mono mt-1 max-h-24 overflow-auto whitespace-pre-wrap text-xs text-muted">{tail.join("\n")}</pre>
                    )}
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ponytail: ext buckets as Sets — O(1) membership, cheapest readable form.
const normExt = (e: string) => e.toLowerCase().replace(/^\./, "");
const IMG = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp"]);
const AUDIO = new Set(["mp3", "wav", "m4a", "flac", "ogg"]);
const VIDEO = new Set(["mp4", "mov", "webm", "mkv", "m4v"]);
const MD = new Set(["md", "markdown", "txt"]);

function ArtifactRow({ a, onDownload }: { a: Artifact; onDownload: () => void }) {
  const type = a.type != null ? String(a.type) : null;
  const title = a.title != null ? String(a.title) : null;
  const status = a.status != null ? String(a.status) : null;
  const hasFields = type || title || status;
  return (
    <div className="row-in flex items-center gap-2 border border-edge bg-void px-2 py-1">
      {hasFields ? (
        <span className="mono flex-1 truncate">
          {type ?? "artifact"}{title && title !== type ? ` — ${title}` : ""}
        </span>
      ) : (
        <span className="mono flex-1 truncate text-muted">{JSON.stringify(a).slice(0, 80)}</span>
      )}
      {status && <span className="label text-muted">{status}</span>}
      <button className="btn" onClick={onDownload}>DOWNLOAD</button>
    </div>
  );
}
