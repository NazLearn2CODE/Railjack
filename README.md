# Orbiter

A locally-hosted **Agentic OS** — an orchestration layer that treats AI agents as
OS processes (scheduling, sandboxing, observability) behind a web dashboard on
`localhost:<port>`.

Built from the blueprint in [`A-project/agentic-os-guide.md`](A-project/agentic-os-guide.md).
Start with [`A-project/index.md`](A-project/index.md) → [`A-project/architecture.md`](A-project/architecture.md).

## Stack

- **Python** — FastAPI + `claude-agent-sdk` + WebSocket; OS core on `asyncio`
- **React 19** + Vite + Tailwind v4 — dashboard (use the `frontend-design` skill)

## Run (once built)

```bash
uvicorn app.main:app --reload --port 8000
```

## Vault

This repo is also an Obsidian vault (`A-project/` · `B-sessions/` · `Z-harvest/`).
MCP config lives in `.claude/settings.json`. Project rules in `CLAUDE.md`.
