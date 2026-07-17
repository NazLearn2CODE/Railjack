---
title: Agent — Railjack project
date: 2026-07-07
updated: 2026-07-18
tags: [bootstrap, agents, railjack]
---

# Agent — Railjack project

This file is read by any non-Claude agent runtime that follows the `AGENTS.md`
convention (ZCode, Codex, Gemini, local models, future agents) when launched
**inside the Railjack project**. It is a **thin pointer** — the actual project
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

## Vault Access Control

**⚠️ CRITICAL: READ-ONLY ACCESS TO CEPHALON VAULT ⚠️**

The vault (`~/Cephalon/`) is READ-ONLY for project agents.

**Allowed:**
- ✅ READ vault files for context
- ✅ Reference vault knowledge when relevant
- ✅ Suggest vault updates to the user

**NEVER:**
- ❌ WRITE, EDIT, or DELETE any vault file
- ❌ Create new vault files
- ❌ Run git commands on the vault
- ❌ Attempt to "fix" or "improve" vault structure

**Rationale:** Vault integrity requires centralized stewardship. Only the user and
Vault Claude (launched from `~/Cephalon`) may modify it. See `~/Cephalon/Cephalon.md`
§ Access control.

## Vault Read-Triggers (read at the moment of need, not upfront)

The vault is READ-ONLY for you, but reading it is the point — it holds prior
projects' scar tissue. When a trigger fires, read the note BEFORE debugging
from scratch:

| When you are... | Read first |
|---|---|
| Writing/fixing tests (esp. pytest, SDK mocks) | `~/Cephalon/10-knowledge/testing-gotchas.md` |
| Fighting TypeScript errors | `~/Cephalon/10-knowledge/typescript-gotchas.md` |
| Using claude-agent-sdk | `~/Cephalon/10-knowledge/claude-agent-sdk-gotchas.md` |
| Adding any external API/LLM/WebSocket integration | f5-connector-scaffold skill |
| Hitting "works at home, not at the office" | f5-drift-doctor skill |
| Unsure where knowledge lives | `~/Cephalon/index.md` (routing map) |

Every time a vault note answers a question or prevents rework, log it:
`vault-assist: [[note-name]] — what it saved`. The inverse too:
`vault-gap: <what you needed that the vault didn't have>`.

## Non-negotiables

- **Two-memory boundary.** Your memory lives in *this* project, not the vault:
  session logs → `B-sessions/` (gitignored, machine-local), decisions →
  `A-project/decisions/`, architecture/plans → `A-project/`. Per-agent local
  memory (if any) holds only behavior notes + pointers into the vault.
- **Safety protocol.** Before any non-trivial change and after completing it,
  invoke f5-vibe-check. After a 3rd failed attempt at the same bug, or when
  starting a HIGH-tier task, invoke f5-stop-digging.
- **Verify before commit.** `.venv/bin/pytest -q` · `cd web && npx tsc --noEmit
  && npm run build` · `.venv/bin/ruff check`.
- **Commit convention.** Commit directly to `main`, one coherent increment per
  commit, conventional-commit message ending with
  `Co-Authored-By: Claude <noreply@anthropic.com>`. Stage only the increment's
  files via **explicit paths**; confirm `git status` shows only the increment
  before committing. (`B-sessions/` is gitignored since the 2026-07-18 pivot —
  the old restore-before-commit rule is obsolete.)
- **Milestone marks.** After completing a milestone, update the table in
  `A-project/index.md` in the SAME commit — a fresh session resumes from
  `A-project/plans/2026-07-18-railjack-hub-build.md` + that table.

## Cross-machine

This is the **home instance** (`~/Coding Projects/Railjack/`, machine "Tawhan",
hostname `bazzite`). A separate **office instance** exists at
`/home/ThePRODUCER/Orbiter/` on Orokin (its own rename is pending, name TBD by
Naz) — each machine works its own. Per-machine modules come from
`config/<machine>.yaml`. See `~/Cephalon/20-projects/railjack.md` (vault,
read-only).

## End of Session

When the session ends:
1. Clean up `B-sessions/` if needed.
2. Ensure project state is committed (git).
3. DO NOT touch the Cephalon vault.

The user (or Vault Claude) will handle vault sync separately.

# currentDate
Today's date is 2026-07-08.
