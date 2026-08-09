# Kanban semantic search — build spec (local RAG, secrecy-safe)

## Goal
Semantic search over Kanban board content (archived **and** active) so you can find
past tasks/boards by **meaning**, not just keyword — e.g. search "real-time sister
presence" and surface the Buzz live-wiring tasks even if those exact words aren't in
them. **Local to Railjack**: indexed in `kanban.db`, embeddings via local Ollama.
Deliberately NOT the shared Cephalon vault RAG, so Tasai cannot surface Kanban boards.

## Stack
- **Embeddings:** `bge-m3` via Ollama (already running on home for cephalon-rag).
  `POST http://localhost:11434/api/embeddings {"model":"bge-m3","prompt":<text>}`
  → `{"embedding":[...1024 floats...]}`.
- **Index store:** a new table in the same `kanban.db` SQLite file the board uses.
- **Search:** cosine similarity, brute-force (numpy) — fine for personal scale
  (hundreds of tasks; sub-100ms). No ANN index needed.

## Schema — add via the existing `_migrate()` in `app/kanban.py`
```sql
CREATE TABLE IF NOT EXISTS kanban_search (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  task_id INTEGER NOT NULL,
  text TEXT NOT NULL,            -- the exact text that was embedded
  embedding BLOB NOT NULL,       -- np.float32 vector → bytes
  indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ks_task ON kanban_search(task_id);
```

## What to embed (per task)
Concatenate, per task: **project name · column title · task title · description · the
task's latest ~5 activity lines**. One embedding per task. The activity lines are what
make search actually useful — they carry the "what happened" context.

## Endpoints (extend `app/kanban.py`)
- `POST /api/kanban/search/index` — rebuild the whole index: iterate all tasks
  (active + archived), embed each, upsert by `task_id` (delete-then-insert per task).
  Idempotent.
- `POST /api/kanban/search/index?project=<id>` — index one project. **Wire this into
  the existing `/project/{id}/archive` endpoint** so a board becomes searchable the
  moment it's archived.
- `GET /api/kanban/search?q=<query>&limit=10` — embed the query (bge-m3), cosine-sim
  against every `kanban_search` row, return top-k:
  `{project_id, project_name, task_id, title, snippet, score}`.

## Frontend (`KanbanPanel.tsx`)
A 🔍 input in the toolbar → on Enter, `GET /api/kanban/search?q=` → results list
(project · title · snippet · score). Clicking a result switches to that project
(and ideally scrolls to the task). Must work for archived boards too.

## Indexing triggers (MVP)
- On **archive** → index that project immediately (archived = searchable).
- A **manual rebuild** (`POST /search/index`) — surface as a small "reindex" action.
- Incremental index-on-task-write = later; MVP is archive-triggered + manual rebuild.

## Secrecy (the whole reason this is "local")
- Index lives in `~/.config/railjack/kanban.db` (home), **not** under `~/Cephalon`.
- Embeddings via **local Ollama** — nothing leaves the machine.
- **Nothing is written to the vault**, so the shared `cephalon-rag` (which Tasai uses)
  cannot surface Kanban content. Acceptance test: after indexing, `ls ~/Cephalon` is
  unchanged.

## Acceptance
- Index the "Buzz — live wiring" board; search `real-time sister presence` → returns
  the subscriber/executor tasks (semantic, not keyword, match).
- Archive a board; search a phrase drawn from its tasks → still found.
- Brute-force cosine over ~100 tasks < 100ms.
- Secrecy check: nothing added under `~/Cephalon`.

## Implementation notes
- Verify Ollama + bge-m3: `curl -s localhost:11434/api/tags | grep bge-m3` (if absent,
  `ollama pull bge-m3`).
- Embedding bytes: `np.array(vec, dtype=np.float32).tobytes()`; to search: load all
  rows, stack into a matrix, L2-normalize, `scores = matrix @ query_norm`.
- Reuse `app/kanban.py`'s `_db()`, `_migrate()`, `_opts()`; keep the new code in
  `app/kanban.py` (or a sibling `app/kanban_search.py` imported by it) — do not add a
  dependency beyond `numpy` + the stdlib `urllib`/`requests` already present.
