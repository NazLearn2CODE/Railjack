---
title: Agent — Orbiter project
date: 2026-07-07
tags: [bootstrap, agents, orbiter]
---

# Agent — Orbiter project

This file is read by any non-Claude agent runtime that follows the `AGENTS.md`
convention (ZCode, Codex, Gemini, local models, future agents) when launched
**inside the Orbiter project**. It is a **thin pointer** — the actual project
rules live in `CLAUDE.md`, once. Read that; this file only tells you the order
and the non-negotiables.

## What you are here

You are a **Project Agent** — project scope only. You are **not** the vault
steward. Build, test, and document *this* project; do not touch the shared
memory vault.

## Bootstrap order (every session, before acting)

1. Read `CodeCompass.md` — the project context manual (what the code can't tell you).
2. Read `A-project/index.md` — project overview + current status + working conventions.
3. Read `CLAUDE.md` — the project's full rules (bootstrap, web-research chain, memory writes, end-of-session).
4. Check `B-sessions/` for recent session logs.

## Non-negotiables

- **Vault is READ-ONLY.** `~/Cephalon/` is the shared long-term memory for all
  agents. You may READ it for context; you may NEVER write, edit, delete, or
  move any vault file, create vault files, or run git on the vault. Only the
  user and Vault Claude (launched from `~/Cephalon`) may modify it. See
  `~/Cephalon/Cephalon.md` § Access control.
- **Two-memory boundary.** Your memory lives in *this* project, not the vault:
  session logs → `B-sessions/`, decisions → `A-project/decisions/`, architecture
  → `A-project/`, harvest → `Z-harvest/`. Per-agent local memory (if any) holds
  only behavior notes + pointers into the vault.
- **Safety protocol.** Before any non-trivial change and after completing it,
  invoke vibe-check. After a 3rd failed attempt at the same bug, or when
  starting a HIGH-tier task, invoke stop-digging.
- **Verify before commit.** `.venv/bin/pytest -q` · `cd web && npx tsc --noEmit
  && npm run build` · `.venv/bin/ruff check`. Always `git restore --staged
  B-sessions/` before committing (runtime logs auto-stage).

## Cross-machine

This is the **home instance** (`~/Coding Projects/Orbiter/`). A separate
**office instance** exists at `/home/ThePRODUCER/Orbiter/` with different scope
— each machine works its own. See `~/Cephalon/20-projects/orbiter.md` (vault,
read-only).
