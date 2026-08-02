# KANBAN — native Railjack board module

A personal Kanban board built **natively** into Railjack (no container, no
external app) — Python/FastAPI over SQLite (Railjack's first database), with a
React panel. Modeled on Kanboard's data model. MVP: projects × columns ×
swimlanes × tasks, drag-drop reorder, inline add, card edit modal.

Kanboard itself was rejected for embedding: it's subpath-hostile (no base-path
config, absolute AJAX URLs break under `/kanboard/`), and cross-origin iframe
is blocked by X-Frame-Options + third-party cookies. A native module is the
clean way to be truly *in* Railjack.

## Files
- `app/kanban.py` — FastAPI router + SQLite schema/access + Kanboard's integer-position renumber rule.
- `frontend/src/components/KanbanPanel.tsx` — the panel (project selector, columns, native HTML5 drag-drop, edit modal).
- `app/main.py` — `from .kanban import router as kanban_router` + `app.include_router(kanban_router)` (before the static catch-all).
- `frontend/src/App.tsx` — `kanban: KanbanPanel` in the `PANELS` map.
- `configs/<machine>.yaml` — the module block (below).

## Installation (on a Railjack instance)

1. **Code is present + wired** (already true on home/tawhan):
   - `app/kanban.py` exists; `app/main.py` imports `kanban_router` and `app.include_router(kanban_router)`.
   - `frontend/src/components/KanbanPanel.tsx` exists; `frontend/src/App.tsx` `PANELS` has `kanban: KanbanPanel`.
2. **Register the module** in the target machine's config YAML (e.g. `configs/tawhan.yaml`), inside the `modules:` list:
   ```yaml
     - id: kanban
       title: KANBAN
       kind: panel
       panel: kanban
       options:
         db_path: ~/.config/railjack/kanban.db
         default_columns: [Backlog, "To Do", "In Progress", Done]
   ```
3. **Build the frontend** (dist is gitignored, so build per machine):
   ```bash
   cd frontend && npm run build        # tsc + vite
   ```
4. **Restart the hub**:
   ```bash
   systemctl --user restart railjack.service
   ```
5. **Database** — created automatically on first `/api/kanban/board` access at
   `options.db_path` (default `~/.config/railjack/kanban.db`). First-boot seeds
   one project ("My Board"), the `default_columns`, and one swimlane ("Default").
   To wipe + re-seed: `rm ~/.config/railjack/kanban.db && systemctl --user restart railjack.service`.
6. **Verify**:
   ```bash
   curl -s localhost:8700/api/kanban/board | python3 -m json.tool   # seeded project + 4 columns, 0 tasks
   ```
   Then open Railjack → **KANBAN** tab. Project selector + columns render.

## Data model (SQLite, stdlib `sqlite3`, no ORM)
- `projects(id, name, is_active, created_at)`
- `columns(id, project_id→projects, title, position, task_limit)` · ordered by `position`
- `swimlanes(id, project_id→projects, name, position, is_active)` · ordered by `position`
- `tasks(id, project_id, column_id, swimlane_id, title, description, position, priority, assignee, due_date, is_active, created_at, completed_at)` · position scoped per `(column_id, swimlane_id)` cell
- **Position rule (Kanboard):** on any move, the target cell's active tasks are renumbered to contiguous `1..N` (insert the moved task at its slot) — no gaps, no fractions.

## API (`/api/kanban/...`)
- `GET /board?project=<id>` → `{projects, active_project, columns, swimlanes, tasks}` (all position-ordered)
- `POST /project {name}` → creates project + default columns + default swimlane
- `POST /column {project_id, title}`
- `POST /task {project_id, column_id, swimlane_id?, title}`
- `PATCH /task/{id} {title?, description?, priority?, assignee?, due_date?}`
- `DELETE /task/{id}` (hard delete)
- `POST /task/{id}/move {column_id, swimlane_id?, before_task_id?}` → renumber rule; `before_task_id=null` appends

## MVP scope / not yet built
- No delete-project / delete-column (add a card/button when needed).
- Single default swimlane is rendered (schema is swimlane-ready; a row UI is a later add).
- No WIP enforcement, Gantt, automation, subtasks, or mobile (Kanboard's extras — add only if the habit sticks).

## Self-test
```bash
.venv/bin/python -m app.kanban     # verifies the renumber rule (insert-before + append) stays contiguous
```
