import { useEffect, useRef, useState } from "react";
import type { FC } from "react";
import { fetchJSON, usePolling } from "../api";
import type { ModuleConfig } from "../store";

// Native Kanban board (backend: app/kanban.py, SQLite). MVP: projects × columns
// × tasks with drag-drop reorder, inline add, a card edit modal. Single default
// swimlane is rendered (schema is swimlane-ready for a later row UI).
// Kanboard's integer-position-renumber-per-cell rule lives server-side.

interface Project { id: number; name: string }
interface Column { id: number; title: string; position: number; task_limit: number | null }
interface Task {
  id: number; column_id: number; title: string; description: string | null;
  position: number; priority: number; assignee: string | null; due_date: string | null;
  started_at: string | null;
  worker_pid: number | null;
  activity: string[];
}
interface Board { projects: Project[]; archived_projects: Project[]; active_project: number | null; columns: Column[]; tasks: Task[] }
interface SearchResult {
  project_id: number;
  project_name: string;
  task_id: number;
  title: string;
  snippet: string;
  score: number;
}

/** "working 14m" / "working 2h5m" / "working 1d" — server started_at is UTC "YYYY-MM-DD HH:MM:SS". */
function elapsed(startedAt: string, now: number): string {
  const s = Math.max(0, Math.floor((now - new Date(startedAt.replace(" ", "T") + "Z").getTime()) / 1000));
  if (s < 60) return "working · just started";
  const m = Math.floor(s / 60);
  if (m < 60) return `working ${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `working ${h}h${m % 60}m`;
  return `working ${Math.floor(h / 24)}d`;
}

const CT = { "Content-Type": "application/json" } as const;
const mut = (url: string, method: string, body?: object) =>
  fetchJSON(url, { method, headers: CT, ...(body ? { body: JSON.stringify(body) } : {}) });

const KanbanPanel: FC<{ module: ModuleConfig }> = () => {
  const [projectId, setProjectId] = useState<number | null>(() => {
    const s = typeof localStorage !== "undefined" && localStorage.getItem("kanban:lastproject");
    return s ? Number(s) : null;
  });
  const { data: board, refetch } = usePolling<Board>(
    `/api/kanban/board${projectId != null ? `?project=${projectId}` : ""}`, 4000,
  );
  const [dragId, setDragId] = useState<number | null>(null);
  const [manualMode, setManualMode] = useState(false); // Mode 2: ▶ = timer only, no agent dispatch
  const [newProject, setNewProject] = useState("");
  const [newCol, setNewCol] = useState("");
  const [addCol, setAddCol] = useState<number | null>(null); // column id being typed into
  const [addText, setAddText] = useState("");
  const [edit, setEdit] = useState<Task | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [reindexing, setReindexing] = useState(false);
  useEffect(() => { const i = setInterval(() => setNow(Date.now()), 30_000); return () => clearInterval(i); }, []);

  const boardRef = useRef<HTMLDivElement>(null);
  const curProj = projectId ?? board?.active_project ?? 0;
  // Position-save: restore a board's horizontal scroll when switching back to it.
  useEffect(() => {
    const el = boardRef.current;
    if (!el) return;
    const restore = () => { el.scrollLeft = Number(localStorage.getItem("kanban:scroll:" + curProj) || 0); };
    restore();
    requestAnimationFrame(restore);  // columns may not be laid out yet on first paint
  }, [curProj]);

  const cols = board?.columns ?? [];
  const tasks = board?.tasks ?? [];
  const curArchived = !!board && (board.archived_projects ?? []).some((p) => p.id === curProj);
  const inCol = (cid: number) => tasks.filter((t) => t.column_id === cid).sort((a, b) => a.position - b.position);

  const doSearch = async () => {
    const q = searchQuery.trim();
    if (!q) return;
    setSearching(true);
    try {
      const res = (await fetchJSON(`/api/kanban/search?q=${encodeURIComponent(q)}`)) as { results: SearchResult[] };
      setSearchResults(res.results || []);
    } catch {
      setSearchResults([]);
    } finally {
      setSearching(false);
    }
  };

  const doReindex = async () => {
    setReindexing(true);
    try {
      await mut("/api/kanban/search/index", "POST");
    } finally {
      setReindexing(false);
    }
  };

  const move = async (taskId: number, columnId: number, before: number | null) => {
    setDragId(null);
    if (taskId === before) return;
    await mut(`/api/kanban/task/${taskId}/move`, "POST", { column_id: columnId, before_task_id: before });
    refetch();
  };
  const addTask = async (columnId: number) => {
    const t = addText.trim();
    if (!t || !board?.active_project) return;
    await mut("/api/kanban/task", "POST", { project_id: board.active_project, column_id: columnId, title: t });
    setAddText(""); setAddCol(null); refetch();
  };
  const addProject = async () => {
    const n = newProject.trim();
    if (!n) return;
    const r = (await mut("/api/kanban/project", "POST", { name: n })) as { id: number };
    setNewProject(""); setProjectId(r.id); refetch();
  };
  const addColumn = async () => {
    const n = newCol.trim();
    if (!n || !board?.active_project) return;
    await mut("/api/kanban/column", "POST", { project_id: board.active_project, title: n });
    setNewCol(""); refetch();
  };
  const saveEdit = async () => {
    if (!edit) return;
    await mut(`/api/kanban/task/${edit.id}`, "PATCH", {
      title: edit.title, description: edit.description, assignee: edit.assignee,
      due_date: edit.due_date, priority: edit.priority,
    });
    setEdit(null); refetch();
  };
  const delTask = async (id: number) => { await mut(`/api/kanban/task/${id}`, "DELETE"); setEdit(null); refetch(); };
  const startTask = async (id: number, manual = false) => { await mut(`/api/kanban/task/${id}/start${manual ? "?manual=1" : ""}`, "POST"); refetch(); };
  const stopTask = async (id: number) => { await mut(`/api/kanban/task/${id}/stop`, "POST"); refetch(); };

  if (!board) return <div className="flex h-full w-full items-center justify-center text-muted">Loading board…</div>;

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      {/* toolbar */}
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-edge px-3 py-1.5">
        <select
          className="border border-edge bg-void px-2 py-0.5 text-sm"
          value={curProj}
          onChange={(e) => { const v = Number(e.target.value); setProjectId(v); localStorage.setItem("kanban:lastproject", String(v)); }}
        >
          <optgroup label="Active">
            {board.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </optgroup>
          {(board.archived_projects ?? []).length > 0 && (
            <optgroup label={`Archived (${board.archived_projects.length})`}>
              {board.archived_projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </optgroup>
          )}
        </select>
        <input
          className="w-36 border border-edge bg-void px-2 py-0.5 text-sm" placeholder="+ project"
          value={newProject} onChange={(e) => setNewProject(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && addProject()}
        />
        <span className="text-edge">·</span>
        <input
          className="w-36 border border-edge bg-void px-2 py-0.5 text-sm" placeholder="+ column"
          value={newCol} onChange={(e) => setNewCol(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && addColumn()}
        />
        <button
          className="mono text-xs px-2 py-0.5 border border-edge"
          style={{ color: manualMode ? "var(--color-signal)" : "var(--color-muted)" }}
          title={manualMode ? "MANUAL: ▶ starts a timer only (no agent). Click for AUTO." : "AUTO: ▶ dispatches an agent. Click for MANUAL (timer only)."}
          onClick={() => setManualMode((m) => !m)}
        >{manualMode ? "⏱ MANUAL" : "▶ AUTO"}</button>
        {curArchived ? (
          <button className="btn btn--compact" onClick={async () => { await mut(`/api/kanban/project/${curProj}/restore`, "POST"); refetch(); }}>⟲ Restore</button>
        ) : (
          <button className="btn btn--compact" onClick={async () => { const nm = board.projects.find((p) => p.id === curProj)?.name ?? "this board"; if (confirm(`Archive "${nm}"? Hides it from the active list (still viewable + searchable).`)) { await mut(`/api/kanban/project/${curProj}/archive`, "POST"); refetch(); } }}>📁 Archive</button>
        )}
        <span className="text-edge">·</span>
        <div className="flex items-center gap-1">
          <input
            className="w-36 border border-edge bg-void px-2 py-0.5 text-sm"
            placeholder={searching ? "searching..." : "🔍 search..."}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void doSearch()}
          />
          <button
            className="mono text-xs px-1.5 py-0.5 border border-edge hover:text-phosphor"
            title="Rebuild RAG search index"
            disabled={reindexing}
            onClick={doReindex}
          >
            {reindexing ? "..." : "⚡ index"}
          </button>
        </div>
      </div>

      {/* board */}
      <div ref={boardRef} onScroll={(e) => localStorage.setItem("kanban:scroll:" + curProj, String(e.currentTarget.scrollLeft))} className="flex min-h-0 flex-1 gap-2 overflow-x-auto p-2">
        {cols.map((c) => (
          <div
            key={c.id}
            className="flex w-60 shrink-0 flex-col border border-edge bg-shade"
            onDragOver={(e) => e.preventDefault()}
            onDrop={() => { if (dragId != null) move(dragId, c.id, null); }}
          >
            <div className="mono shrink-0 border-b border-edge px-2 py-1 text-xs" style={{ color: "var(--color-signal)" }}>
              {c.title} <span className="text-muted">({inCol(c.id).length})</span>
            </div>
            <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto p-1">
              {inCol(c.id).map((t) => (
                <div
                  key={t.id}
                  draggable
                  onDragStart={() => setDragId(t.id)}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={(e) => { e.stopPropagation(); if (dragId != null && dragId !== t.id) move(dragId, c.id, t.id); }}
                  onClick={() => setEdit({ ...t })}
                  className="cursor-pointer border border-edge bg-void px-2 py-1 text-sm hover:border-phosphor"
                  style={t.started_at ? { borderColor: "var(--color-signal)" } : undefined}
                >
                  <div className="flex items-start justify-between gap-1">
                    <span>{t.title}</span>
                    <button
                      className="mono text-xs shrink-0"
                      style={{ color: t.started_at ? "var(--color-signal)" : "var(--color-muted)" }}
                      title={t.started_at ? "Stop timer & worker" : (manualMode ? "Start timer only (MANUAL)" : "Start — dispatch agent (AUTO)")}
                      onClick={(e) => { e.stopPropagation(); void (t.started_at ? stopTask(t.id) : startTask(t.id, manualMode)); }}
                    >{t.started_at ? "⏸" : "▶"}</button>
                  </div>
                  {t.started_at && (
                    <div className="mono text-xs flex items-center justify-between gap-1" style={{ color: "var(--color-signal)" }}>
                      <span>▸ {elapsed(t.started_at, now)}</span>
                      {t.worker_pid && <span className="text-[10px]" style={{ color: "var(--color-phosphor)" }}>⚙ agent</span>}
                    </div>
                  )}
                  {t.activity?.map((a, i) => (
                    <div key={i} className="mono text-xs" style={{ color: "var(--color-phosphor-dim)" }}>· {a}</div>
                  ))}
                  {(t.assignee || t.due_date) && (
                    <div className="mono text-xs text-muted">{t.assignee}{t.due_date ? ` · ${t.due_date}` : ""}</div>
                  )}
                </div>
              ))}
              {addCol === c.id ? (
                <input
                  autoFocus className="border border-edge bg-void px-2 py-1 text-sm" placeholder="task title"
                  value={addText} onChange={(e) => setAddText(e.target.value)}
                  onBlur={() => { if (addText.trim()) addTask(c.id); else setAddCol(null); }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") addTask(c.id);
                    if (e.key === "Escape") { setAddCol(null); setAddText(""); }
                  }}
                />
              ) : (
                <button
                  className="px-1 py-0.5 text-left text-xs text-muted hover:text-phosphor"
                  onClick={() => { setAddCol(c.id); setAddText(""); }}
                >+ add task</button>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* edit modal */}
      {edit && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center"
          style={{ background: "rgba(0,0,0,0.6)" }}
          onClick={() => setEdit(null)}
        >
          <div className="hud hud--bracket flex w-[420px] flex-col gap-2 bg-void p-3" onClick={(e) => e.stopPropagation()}>
            <div className="panel-title">Edit task</div>
            <input
              className="border border-edge bg-shade px-2 py-1 text-sm" value={edit.title}
              onChange={(e) => setEdit({ ...edit, title: e.target.value })}
            />
            <textarea
              className="border border-edge bg-shade px-2 py-1 text-sm" rows={3} placeholder="description"
              value={edit.description ?? ""}
              onChange={(e) => setEdit({ ...edit, description: e.target.value })}
            />
            <div className="flex gap-2">
              <input
                className="w-24 border border-edge bg-shade px-2 py-1 text-sm" placeholder="assignee"
                value={edit.assignee ?? ""}
                onChange={(e) => setEdit({ ...edit, assignee: e.target.value })}
              />
              <input
                className="w-32 border border-edge bg-shade px-2 py-1 text-sm" type="date" title="due date"
                value={edit.due_date ?? ""}
                onChange={(e) => setEdit({ ...edit, due_date: e.target.value })}
              />
              <input
                className="w-16 border border-edge bg-shade px-2 py-1 text-sm" type="number" title="priority"
                value={edit.priority}
                onChange={(e) => setEdit({ ...edit, priority: Number(e.target.value) })}
              />
            </div>
            <div className="flex gap-2">
              <button className="btn" onClick={saveEdit}>Save</button>
              <button className="btn btn--crit" onClick={() => delTask(edit.id)}>Delete</button>
              <button className="btn" onClick={() => setEdit(null)}>Cancel</button>
            </div>
          </div>
        </div>
      )}

      {/* search results modal */}
      {searchResults != null && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center pt-16"
          style={{ background: "rgba(0,0,0,0.6)" }}
          onClick={() => setSearchResults(null)}
        >
          <div
            className="hud hud--bracket flex max-h-[80vh] w-[600px] flex-col gap-2 bg-void p-3 overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-edge pb-2">
              <div className="panel-title">
                Search Results ({searchResults.length})
              </div>
              <button
                className="text-xs text-muted hover:text-phosphor"
                onClick={() => setSearchResults(null)}
              >
                ✕ Close
              </button>
            </div>
            <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto pr-1">
              {searchResults.length === 0 ? (
                <div className="py-4 text-center text-sm text-muted">
                  No matching tasks found.
                </div>
              ) : (
                searchResults.map((res) => (
                  <div
                    key={res.task_id}
                    onClick={() => {
                      setProjectId(res.project_id);
                      localStorage.setItem("kanban:lastproject", String(res.project_id));
                      setSearchResults(null);
                    }}
                    className="cursor-pointer border border-edge bg-shade p-2 text-sm hover:border-phosphor"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold text-phosphor">
                        [{res.project_name}] {res.title}
                      </span>
                      <span className="mono text-xs text-muted">
                        score: {res.score}
                      </span>
                    </div>
                    <div className="mono mt-1 text-xs text-muted truncate">
                      {res.snippet}
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default KanbanPanel;
