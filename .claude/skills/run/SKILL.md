---
name: run
description: Launch the Orbiter gateway + dashboard and run the browser smoke test. Use when asked to run, start, smoke-test, screenshot, or verify the app end-to-end.
---

# Run Orbiter

Two-process dev setup; the browser drives the dashboard end-to-end. Gateway on
`:8000` (FastAPI + claude-agent-sdk), Vite on `:5173` proxying `/api` + `/ws` to it.

## Prerequisites (one-time)

- `.venv` with backend deps — `python -m venv .venv && .venv/bin/pip install -e '.[dev]'`
- `claude` CLI on PATH (the SDK spawns it as a subprocess)
- `.env` with `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` (z.ai or Anthropic)
- `web/node_modules` — `cd web && npm install`

## Start both servers

```bash
# terminal 1 — gateway (creds must be in the shell env; app/ does not load .env)
set -a; . ./.env; set +a; .venv/bin/uvicorn app.main:app --port 8000

# terminal 2 — Vite
cd web && npm run dev
```

Wait for both: `curl -s :8000/api/health` → `{"status":"OK"}`; `curl -s localhost:5173` → 200.

**Gotchas (both hit during setup):**
- **Vite binds IPv6 `[::1]:5173` only** — drive the browser at `http://localhost:5173/`
  (resolves to ::1). `127.0.0.1:5173` returns connection-refused.
- **The gateway never calls `load_dotenv()`** — source `.env` in the launching
  shell (`set -a; . ./.env; set +a`) or the SDK has no key.

## Smoke test

```bash
cd scripts && npm install && node smoke.mjs
```

Drives: shell render → dispatch `orbiter-ok` → WS token stream → completion
(`STATUS=COMPLETED`, `TOKENS>0`) → Bash approval card → APPROVE → run completes.
Screenshots land in `scripts/screenshots/`. Prints
`RESULT approvalShown=true consoleErrors=0` and exits non-zero on console errors.

Playwright reuses the cached headless-shell in `~/.cache/ms-playwright` (pinned via
`executablePath` in `smoke.mjs`) to skip a ~150MB browser download; if none is
cached, run `npx playwright install chromium`.

## Stop

`pkill -f uvicorn; pkill -f vite` (or kill the background PIDs).
