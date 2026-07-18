# Task: Railjack NotebookLM module — TESTS ONLY

Write ONE new file: `tests/test_notebooklm.py`. The backend module
`app/notebooklm.py` is ALREADY IMPLEMENTED and must NOT be changed. Do not
touch any other file. Do not commit.

## Study first
- `app/notebooklm.py` — the module under test (read it fully; match the ACTUAL
  argv, endpoint names, HTTPException codes, and Job dataclass you find there).
- `tests/test_comfyui.py` — canonical test idioms. Mirror them exactly:
  - No pytest-asyncio. Wrap async calls in `asyncio.run(...)`.
  - Mock subprocess with the `_Stream` / `_FakeProc` / `_exec` helper pattern
    (monkeypatch `asyncio.create_subprocess_exec`).
  - Patch module `_OPTS` via `monkeypatch.setattr(notebooklm, "_OPTS", {...})`.
- `tests/conftest.py` — shared fixtures (autouse cache reset already present).

## Required cases (all subprocess/httpx MOCKED — suite must pass with no CLI/auth)
1. **Catalog shape** — `notebooklm.catalog()` returns `{"types": [...]}`; every
   type has an `id`, `ext`, `needs_instructions` bool, and a `groups` list;
   `data-table` has `needs_instructions is True`; `mind-map` has empty groups.
2. **Job to_dict shape** — `Job(...).to_dict()` keys are exactly
   `{id, kind, label, status, progress, output_paths, error, logs}` (no `proc`,
   no `cancel`); logs serialize to a list.
3. **Alphabetical notebook sort** — monkeypatch `notebooklm._run_cli` (async) to
   return an unsorted `{"notebooks": [...]}`; assert `_cached_notebooks(refresh=True)`
   returns titles sorted case-insensitively. Reset `notebooklm._NB_CACHE = None`
   first so the cache can't leak between tests.
4. **Delete-confirm gate** — with the notebook cache primed (set
   `notebooklm._NB_CACHE` to a list containing `{"id": "n1", "title": "My NB"}`):
   - mismatch `confirm_title` → `HTTPException` 400, and the delete argv is NOT run;
   - match → the delete CLI runs (assert via a monkeypatched `_run_cli` that records
     the argv it was called with, and that it contains `notebook`, `delete`, `n1`).
5. **generate-409** — put a `Job(kind="generate", status="running")` in
   `notebooklm._JOBS`; calling `generate(...)` for a valid type raises
   `HTTPException` 409. Also assert a non-generate running job (e.g. kind
   "research") does NOT trigger the 409 (it should get past the guard — reach the
   catalog/title path; you may monkeypatch `_notebook_title` to avoid CLI).
6. **generate validation** — unknown type → 400; an unknown flag or invalid value
   for a known type → 400 (call `notebooklm._validate_generate` directly).
7. **outputs_file confinement** — `outputs_file(path="/etc/passwd")` → 403
   (point `_OPTS["output_dir"]` at a tmp dir first).
8. **add_source path confinement** — with `_OPTS["browse_root"]` set to a tmp dir,
   `add_source("n1", SourceBody(path="/etc/passwd"))` → `HTTPException` 400
   ("outside browse_root"); a `SourceBody(url="https://x")` spawns a job
   (returns a dict with an `id`). Clear `notebooklm._JOBS` after.

Keep tests independent: reset `_JOBS`, `_NB_CACHE`, and `_AUTH_CACHE` where a case
depends on their state. Match the module's real names — if a helper is named
differently than above, use the real name (read the source; do not invent).

## Verify before finishing (MUST run and pass)
```bash
cd "/var/home/NAZ/Coding Projects/Railjack"
.venv/bin/python -m pytest -q
```
Report: the new test count, and full pytest summary line. If a permission gate
blocks the command, say so explicitly.
