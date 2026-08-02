"""Railjack KANBAN module — a native Kanban board (SQLite, no ORM).

A personal board modeled on Kanboard's essentials: projects × columns ×
swimlanes × tasks, with Kanboard's integer-position-renumber-per-cell rule
(no gaps, no fractions — bug-proof at personal scale). Railjack's first
database: stdlib ``sqlite3``, one file, no server, no migrations framework.

REST-only via the hub; the panel is ``frontend/src/components/KanbanPanel.tsx``.
Config in ``configs/tawhan.yaml`` → ``options.db_path`` / ``options.default_columns``.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .config import CONFIG

router = APIRouter()

# Schema is applied once per process (IF NOT EXISTS), then the default board is
# seeded if empty. _initialized guards across the FastAPI threadpool.
_init_lock = threading.Lock()
_initialized = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS swimlanes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  position INTEGER NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS columns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  position INTEGER NOT NULL,
  task_limit INTEGER
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  swimlane_id INTEGER NOT NULL REFERENCES swimlanes(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT,
  position INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0,
  assignee TEXT,
  due_date TEXT,
  started_at TEXT,
  worker_pid INTEGER,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  ts TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS activity_task_ts ON activity(task_id, ts);
"""


def _opts() -> dict:
    """This module's options: block, read fresh each call (cf. thailandnow.py)."""
    for m in CONFIG.modules:
        if m.id == "kanban":
            return m.options or {}
    return {}


def _db_path() -> Path:
    return Path(os.path.expanduser(_opts().get("db_path", "~/.config/railjack/kanban.db")))


@contextmanager
def _db():
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    global _initialized
    if not _initialized:
        with _init_lock:
            if not _initialized:
                conn.executescript(_SCHEMA)
                _migrate(conn)
                _seed_if_empty(conn)
                conn.commit()
                _initialized = True
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _default_columns() -> list[str]:
    return list(_opts().get("default_columns") or ["Backlog", "To Do", "In Progress", "Done"])


def _seed_if_empty(conn: sqlite3.Connection) -> None:
    """First-boot: one default project + default columns + one default swimlane."""
    if conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]:
        return
    cur = conn.cursor()
    cur.execute("INSERT INTO projects (name) VALUES (?)", ("My Board",))
    pid = cur.lastrowid
    cur.execute(
        "INSERT INTO swimlanes (project_id, name, position) VALUES (?, ?, ?)",
        (pid, "Default", 1),
    )
    for i, title in enumerate(_default_columns(), start=1):
        cur.execute(
            "INSERT INTO columns (project_id, title, position) VALUES (?, ?, ?)",
            (pid, title, i),
        )


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column adds for existing DBs (CREATE TABLE only applies to fresh ones)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "started_at" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN started_at TEXT")
    if "worker_pid" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN worker_pid INTEGER")


def _default_swimlane(conn: sqlite3.Connection, project_id: int) -> int:
    row = conn.execute(
        "SELECT id FROM swimlanes WHERE project_id=? ORDER BY position LIMIT 1",
        (project_id,),
    ).fetchone()
    return row[0] if row else 0


def _rowdict(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}


def _place(
    conn: sqlite3.Connection,
    task_id: int,
    column_id: int,
    swimlane_id: int,
    before_task_id: int | None,
) -> None:
    """Move ``task_id`` into the ``(column_id, swimlane_id)`` cell, inserted
    before ``before_task_id`` (or appended when None/absent), then renumber the
    whole cell contiguously 1..N — Kanboard's rule."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE tasks SET column_id=?, swimlane_id=? WHERE id=?",
        (column_id, swimlane_id, task_id),
    )
    rows = cur.execute(
        "SELECT id FROM tasks WHERE column_id=? AND swimlane_id=? AND is_active=1 "
        "AND id!=? ORDER BY position, id",
        (column_id, swimlane_id, task_id),
    ).fetchall()
    ids = [r[0] for r in rows]
    if before_task_id is None or before_task_id not in ids:
        ids.append(task_id)
    else:
        ids.insert(ids.index(before_task_id), task_id)
    for pos, tid in enumerate(ids, start=1):
        cur.execute("UPDATE tasks SET position=? WHERE id=?", (pos, tid))


CLAUDE_BIN = "/home/NAZ/.local/bin/claude"


def _claude_env() -> dict[str, str]:
    env = os.environ.copy()
    key = env.get("ZAI_API_KEY")
    env_file = Path.home() / ".config" / "railjack" / "env"
    if not key and env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("ZAI_API_KEY="):
                key = line.split("=", 1)[1].strip()
                break
    if key:
        env["ANTHROPIC_BASE_URL"] = "https://api.z.ai/api/anthropic"
        env["ANTHROPIC_AUTH_TOKEN"] = key
    return env


def _worker_prompt(
    task_id: int, project_name: str, title: str, description: str | None, done_col_id: int
) -> str:
    desc_str = f"\n{description.strip()}" if description and description.strip() else ""
    return (
        f'You are autonomously working Kanban task #{task_id} (project "{project_name}"): {title}.{desc_str}\n'
        'Goal: complete this task for real, using your tools (Bash, Read, Write, Edit).\n'
        'After each meaningful step, post a SHORT (≤80 char) progress line:\n'
        f'  curl -s -X POST http://localhost:8700/api/kanban/task/{task_id}/activity -H \'Content-Type: application/json\' -d \'{{"text":"<what you just did>"}}\'\n'
        'When the task is FULLY complete:\n'
        f'  curl -s -X POST http://localhost:8700/api/kanban/task/{task_id}/move -H \'Content-Type: application/json\' -d \'{{"column_id":{done_col_id},"before_task_id":null}}\'\n'
        f'  curl -s -X POST http://localhost:8700/api/kanban/task/{task_id}/stop\n'
        'If you cannot complete it, post a line saying why and stop (leave it in progress).\n'
        'Be efficient; don\'t post trivia; stop once done.'
    )


# Worker lifecycle: track dispatched Popen handles (+ monotonic start time) so a
# background thread can (a) poll() — reap exactly those children when they exit
# (scoped: never waitpid(-1), which would steal railjack's other subprocesses'
# exit status), and (b) enforce a max-runtime watchdog that kills a worker past
# the cap (catches glm-5.2 stalls where the agent never reaches move-to-Done).
# Value: (proc, started_monotonic).
_workers: dict[int, tuple[subprocess.Popen, float]] = {}
_worker_lock = threading.Lock()
_reaper_started = False


def _watchdog_max_minutes() -> int:
    """Max minutes a worker may run before the watchdog kills it (config, default 20)."""
    try:
        return max(1, int(_opts().get("watchdog_max_minutes", 20)))
    except (TypeError, ValueError):
        return 20


def _watchdog_kill(task_id: int, proc: subprocess.Popen) -> None:
    """Kill a worker that exceeded the cap, clear its state, leave an explainer line."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    cap = _watchdog_max_minutes()
    try:
        with _db() as conn:
            conn.execute(
                "UPDATE tasks SET started_at=NULL, worker_pid=NULL WHERE id=?", (task_id,)
            )
            conn.execute(
                "INSERT INTO activity (task_id, text) VALUES (?, ?)",
                (task_id, f"watchdog: ran past {cap}m, stopped"),
            )
    except Exception:
        pass  # best-effort; the kill already happened


def _ensure_reaper() -> None:
    """Start the worker-reaper + watchdog daemon thread once (idempotent)."""
    global _reaper_started
    if _reaper_started:
        return
    with _worker_lock:
        if _reaper_started:
            return
        _reaper_started = True

    def _loop() -> None:
        while True:
            time.sleep(3)
            with _worker_lock:
                items = list(_workers.items())
            now = time.monotonic()
            cap_s = _watchdog_max_minutes() * 60
            for tid, (proc, started) in items:
                if proc.poll() is not None:  # reaps this child; None while still alive
                    with _worker_lock:
                        _workers.pop(tid, None)
                    continue
                if cap_s and now - started > cap_s:  # watchdog: runtime exceeded
                    _watchdog_kill(tid, proc)

    threading.Thread(target=_loop, daemon=True, name="kanban-worker-reaper").start()


# ---- request models ----

class NewProject(BaseModel):
    name: str


class NewColumn(BaseModel):
    project_id: int
    title: str


class NewTask(BaseModel):
    project_id: int
    column_id: int
    swimlane_id: int | None = None
    title: str


class TaskPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: int | None = None
    assignee: str | None = None
    due_date: str | None = None


class MoveTask(BaseModel):
    column_id: int
    swimlane_id: int | None = None
    before_task_id: int | None = None  # insert before this id; None = append


class ActivityLine(BaseModel):
    text: str


# ---- endpoints ----

@router.get("/api/kanban/board")
def board(project: int | None = None) -> dict:
    with _db() as conn:
        projects = [
            _rowdict(r)
            for r in conn.execute(
                "SELECT id, name, is_active FROM projects ORDER BY id"
            ).fetchall()
        ]
        empty = {"projects": projects, "active_project": None, "columns": [], "swimlanes": [], "tasks": []}
        if not projects:
            return empty
        active = project if any(p["id"] == project for p in projects) else projects[0]["id"]
        columns = [
            _rowdict(r)
            for r in conn.execute(
                "SELECT id, title, position, task_limit FROM columns WHERE project_id=? ORDER BY position",
                (active,),
            ).fetchall()
        ]
        swimlanes = [
            _rowdict(r)
            for r in conn.execute(
                "SELECT id, name, position, is_active FROM swimlanes WHERE project_id=? ORDER BY position",
                (active,),
            ).fetchall()
        ]
        tasks = [
            _rowdict(r)
            for r in conn.execute(
                "SELECT id, column_id, swimlane_id, title, description, position, priority, "
                "assignee, due_date, started_at, worker_pid, is_active, completed_at FROM tasks "
                "WHERE project_id=? AND is_active=1 ORDER BY column_id, swimlane_id, position",
                (active,),
            ).fetchall()
        ]
        # Check process liveness for task workers
        for t in tasks:
            w_pid = t.get("worker_pid")
            if w_pid is not None:
                try:
                    os.kill(w_pid, 0)
                except ProcessLookupError:
                    t["worker_pid"] = None
                except OSError:
                    pass
        # Live activity feed per task: prune anything older than a minute (so a card
        # with no fresh line shows nothing), then attach the latest ≤2 lines each.
        conn.execute("DELETE FROM activity WHERE ts < datetime('now','-1 minute')")
        tids = [t["id"] for t in tasks]
        by_task: dict[int, list[str]] = {}
        if tids:
            ph = ",".join("?" * len(tids))
            for r in conn.execute(
                f"SELECT task_id, text FROM activity WHERE task_id IN ({ph}) ORDER BY ts DESC",
                tids,
            ).fetchall():
                by_task.setdefault(r[0], []).append(r[1])
        for t in tasks:
            t["activity"] = list(reversed(by_task.get(t["id"], [])[:2]))
        return {
            "projects": projects,
            "active_project": active,
            "columns": columns,
            "swimlanes": swimlanes,
            "tasks": tasks,
        }


@router.post("/api/kanban/project")
def create_project(req: NewProject) -> dict:
    name = req.name.strip() or "Untitled"
    with _db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO projects (name) VALUES (?)", (name,))
        pid = cur.lastrowid
        cur.execute(
            "INSERT INTO swimlanes (project_id, name, position) VALUES (?, ?, ?)",
            (pid, "Default", 1),
        )
        for i, title in enumerate(_default_columns(), start=1):
            cur.execute(
                "INSERT INTO columns (project_id, title, position) VALUES (?, ?, ?)",
                (pid, title, i),
            )
        return {"id": pid, "name": name}


@router.post("/api/kanban/column")
def create_column(req: NewColumn) -> dict:
    title = req.title.strip() or "Column"
    with _db() as conn:
        mx = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM columns WHERE project_id=?",
            (req.project_id,),
        ).fetchone()[0]
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO columns (project_id, title, position) VALUES (?, ?, ?)",
            (req.project_id, title, mx + 1),
        )
        return {"id": cur.lastrowid, "title": title, "position": mx + 1}


@router.post("/api/kanban/task")
def create_task(req: NewTask) -> dict:
    title = req.title.strip() or "Untitled"
    with _db() as conn:
        sid = req.swimlane_id or _default_swimlane(conn, req.project_id)
        mx = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM tasks WHERE column_id=? AND swimlane_id=?",
            (req.column_id, sid),
        ).fetchone()[0]
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tasks (project_id, column_id, swimlane_id, title, position) "
            "VALUES (?, ?, ?, ?, ?)",
            (req.project_id, req.column_id, sid, title, mx + 1),
        )
        return {"id": cur.lastrowid}


@router.patch("/api/kanban/task/{task_id}")
def patch_task(task_id: int, req: TaskPatch) -> dict:
    # Explicit field access (pydantic-version-proof); keys are model-controlled → safe in SQL.
    fields: dict[str, object] = {}
    for k in ("title", "description", "priority", "assignee", "due_date"):
        v = getattr(req, k)
        if v is not None:
            fields[k] = v
    if not fields:
        raise HTTPException(400, "no fields to update")
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [task_id]
    with _db() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE tasks SET {sets} WHERE id=?", vals)
        if cur.rowcount == 0:
            raise HTTPException(404, "task not found")
        return {"ok": True}


@router.delete("/api/kanban/task/{task_id}")
def delete_task(task_id: int) -> dict:
    with _db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "task not found")
        return {"ok": True}


@router.post("/api/kanban/task/{task_id}/move")
def move_task(task_id: int, req: MoveTask) -> dict:
    with _db() as conn:
        row = conn.execute("SELECT project_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "task not found")
        sid = req.swimlane_id or _default_swimlane(conn, row[0])
        _place(conn, task_id, req.column_id, sid, req.before_task_id)
        return {"ok": True}


@router.post("/api/kanban/task/{task_id}/start")
def start_task(task_id: int) -> dict:
    """Stamp started_at = now (UTC) and auto-dispatch an autonomous worker process if none running."""
    with _db() as conn:
        row = conn.execute(
            "SELECT t.id, t.title, t.description, t.project_id, t.worker_pid, p.name AS project_name "
            "FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.id=?",
            (task_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "task not found")

        w_pid = row["worker_pid"]
        if w_pid is not None:
            try:
                os.kill(w_pid, 0)
                return {
                    "ok": True,
                    "already_running": True,
                    "worker_pid": w_pid,
                    "started_at": conn.execute(
                        "SELECT started_at FROM tasks WHERE id=?", (task_id,)
                    ).fetchone()[0],
                }
            except ProcessLookupError:
                pass
            except OSError:
                return {
                    "ok": True,
                    "already_running": True,
                    "worker_pid": w_pid,
                    "started_at": conn.execute(
                        "SELECT started_at FROM tasks WHERE id=?", (task_id,)
                    ).fetchone()[0],
                }

        done_row = conn.execute(
            "SELECT id FROM columns WHERE project_id=? AND title='Done' ORDER BY position LIMIT 1",
            (row["project_id"],),
        ).fetchone()
        if not done_row:
            done_row = conn.execute(
                "SELECT id FROM columns WHERE project_id=? ORDER BY position DESC LIMIT 1",
                (row["project_id"],),
            ).fetchone()
        done_col_id = done_row[0] if done_row else 0

        prompt = _worker_prompt(
            task_id=row["id"],
            project_name=row["project_name"],
            title=row["title"],
            description=row["description"],
            done_col_id=done_col_id,
        )

        proc = subprocess.Popen(
            [CLAUDE_BIN, "-p", prompt, "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep"],
            env=_claude_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _ensure_reaper()
        with _worker_lock:
            _workers[task_id] = (proc, time.monotonic())

        cur = conn.cursor()
        cur.execute(
            "UPDATE tasks SET started_at=datetime('now'), worker_pid=? WHERE id=?",
            (proc.pid, task_id),
        )

        return {
            "ok": True,
            "worker_pid": proc.pid,
            "started_at": conn.execute(
                "SELECT started_at FROM tasks WHERE id=?", (task_id,)
            ).fetchone()[0],
        }


@router.post("/api/kanban/task/{task_id}/stop")
def stop_task(task_id: int) -> dict:
    with _db() as conn:
        row = conn.execute("SELECT worker_pid FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "task not found")
        w_pid = row[0]
        if w_pid is not None:
            try:
                pgid = os.getpgid(w_pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        cur = conn.cursor()
        cur.execute("UPDATE tasks SET started_at=NULL, worker_pid=NULL WHERE id=?", (task_id,))
        return {"ok": True}


@router.post("/api/kanban/task/{task_id}/activity")
def post_activity(task_id: int, req: ActivityLine) -> dict:
    """Append a progress line to a task's live feed (the working agent posts these).
    Lines auto-expire after one minute — pruned on board read, so a card with no
    fresh line shows nothing."""
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "empty activity line")
    with _db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO activity (task_id, text) VALUES (?, ?)", (task_id, text))
        return {"ok": True}


if __name__ == "__main__":
    # Self-check the non-trivial logic (renumber rule) against a throwaway DB.
    # NOTE: does NOT call _db() — that would touch the real configured db_path.
    p = Path("/tmp/_kanban_selftest.db")
    p.unlink(missing_ok=True)
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    pid = c.execute("INSERT INTO projects (name) VALUES ('P')").lastrowid
    sid = c.execute("INSERT INTO swimlanes (project_id, name, position) VALUES (?, 'D', 1)", (pid,)).lastrowid
    cid = c.execute("INSERT INTO columns (project_id, title, position) VALUES (?, 'C', 1)", (pid,)).lastrowid
    for t in ("A", "B", "C", "D"):
        c.execute(
            "INSERT INTO tasks (project_id, column_id, swimlane_id, title, position) VALUES (?,?,?,?,?)",
            (pid, cid, sid, t, 0),
        )
    c.execute("UPDATE tasks SET position = id")  # 1..4 by autoincrement id
    c.commit()
    # move task 1 (A) before task 3 (C) → order B, A, C, D, positions contiguous
    _place(c, 1, cid, sid, 3)
    order = [r[0] for r in c.execute(
        "SELECT title FROM tasks WHERE column_id=? ORDER BY position", (cid,))]
    assert order == ["B", "A", "C", "D"], order
    positions = [r[0] for r in c.execute(
        "SELECT position FROM tasks WHERE column_id=? ORDER BY position", (cid,))]
    assert positions == [1, 2, 3, 4], positions
    # append task 1 (A) to end (before_task_id=None) → B, C, D, A
    _place(c, 1, cid, sid, None)
    order2 = [r[0] for r in c.execute(
        "SELECT title FROM tasks WHERE column_id=? ORDER BY position", (cid,))]
    assert order2 == ["B", "C", "D", "A"], order2

    # Verify migration helper & worker prompt helper
    _migrate(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "worker_pid" in cols, "worker_pid column missing after migration"
    prompt = _worker_prompt(1, "Test Project", "Fix bug", "Detailed desc", 99)
    assert 'task #1 (project "Test Project"): Fix bug' in prompt
    assert "column_id\":99" in prompt
    env = _claude_env()
    assert isinstance(env, dict)

    c.close()
    p.unlink()
    print("kanban self-check OK: renumber rule, migration, prompt & env checks passed")

