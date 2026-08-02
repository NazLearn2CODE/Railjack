import { useEffect, useState } from "react";
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
  activity: string[];
}
interface Board { projects: Project[]; active_project: number | null; columns: Column[]; tasks: Task[] }

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
  const [projectId, setProjectId] = useState<number | null>(null);
  const { data: board, refetch } = usePolling<Board>(
    `/api/kanban/board${projectId != null ? `?project=${projectId}` : ""}`, 4000,
  );
  const [dragId, setDragId] = useState<number | null>(null);
  const [newProject, setNewProject] = useState("");
  const [newCol, setNewCol] = useState("");
  const [addCol, setAddCol] = useState<number | null>(null); // column id being typed into
  const [addText, setAddText] = useState("");
  const [edit, setEdit] = useState<Task | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const i = setInterval(() => setNow(Date.now()), 30_000); return () => clearInterval(i); }, []);

  const cols = board?.columns ?? [];
  const tasks = board?.tasks ?? [];
  const inCol = (cid: number) => tasks.filter((t) => t.column_id === cid).sort((a, b) => a.position - b.position);

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
  const startTask = async (id: number) => { await mut(`/api/kanban/task/${id}/start`, "POST"); refetch(); };
  const stopTask = async (id: number) => { await mut(`/api/kanban/task/${id}/stop`, "POST"); refetch(); };

  if (!board) return <div className="flex h-full w-full items-center justify-center text-muted">Loading board…</div>;

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      {/* toolbar */}
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-edge px-3 py-1.5">
        <select
          className="border border-edge bg-void px-2 py-0.5 text-sm"
          value={projectId ?? board.active_project ?? 0}
          onChange={(e) => setProjectId(Number(e.target.value))}
        >
          {board.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
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
      </div>

      {/* board */}
      <div className="flex min-h-0 flex-1 gap-2 overflow-x-auto p-2">
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
                      title={t.started_at ? "Stop timer" : "Start timer — mark as being worked on"}
                      onClick={(e) => { e.stopPropagation(); void (t.started_at ? stopTask(t.id) : startTask(t.id)); }}
                    >{t.started_at ? "⏸" : "▶"}</button>
                  </div>
                  {t.started_at && (
                    <div className="mono text-xs" style={{ color: "var(--color-signal)" }}>▸ {elapsed(t.started_at, now)}</div>
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
    </div>
  );
};

export default KanbanPanel;
