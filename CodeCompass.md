# Code Compass

> Copy this file to your project root. Reference it from `CLAUDE.md`: `For context, read CodeCompass.md first, then CLAUDE.md`

This manual structures project knowledge to save context window and speed up lengthy coding sessions.
Your local `CLAUDE.md` reads this first, then creates necessary `.md` files as needed.

---

## Core Philosophy

**One source of truth, not scattered knowledge.**
- Context lives here, not in conversation history
- Update this file when architecture changes
- Link to docs, don't duplicate them
- Keep it under 500 words — drill down only when needed

---

## Quick Start (Read This First)

### What This Project Does

<!-- ONE sentence: what problem does this codebase solve? -->

### Current State

**Working on:** <!-- What's being built right now -->
**Next up:** <!-- What's blocked or planned next -->
**Last deployed:** <!-- Date/commit of last production release -->

### Tech Stack

- **Language:** <!-- Python, JS, Go, etc. -->
- **Framework:** <!-- React, FastAPI, etc. -->
- **Database:** <!-- PostgreSQL, MongoDB, etc. -->
- **Key deps:** <!-- 3-5 critical libraries -->

---

## Code Structure Map

### Entry Points

| What | Where | What it does |
|------|-------|--------------|
| Main app | `path/to/main.py` | Bootstraps server, registers routes |
| CLI entry | `path/to/cli.ts` | Handles command-line interface |
| Worker | `path/to/worker/` | Background job processing |

### Core Domains

<!-- Group by feature, not layer. Avoid "controllers", "models" — use what the domain IS. -->

| Domain | Location | Responsibility | Key files |
|--------|----------|-----------------|-----------|
| Auth | `src/auth/` | Login, JWT, permissions | `login.ts`, `middleware.ts` |
| Payments | `src/billing/` | Stripe webhooks, invoicing | `webhook.ts`, `stripe.ts` |
| <!-- More domains --> | | | |

### Shared Utilities

| Utility | Where | When to use |
|---------|-------|-------------|
| Error handling | `src/lib/errors.ts` | All API responses |
| Logging | `src/lib/logger.ts` | Structured logs with context |
| DB client | `src/db/client.ts` | Query execution |

### Convention: Folder by Layer vs Folder by Feature

This project uses: **[FEATURE-LAYER / LAYER-FEATURE / MIXED]**

<!-- Pick one and explain why -->
- FEATURE-LAYER: `src/auth/{controllers,models,services}/` — prefer this for new features
- LAYER-FEATURE: `src/controllers/auth/`, `src/models/auth/` — legacy, avoid adding
- MIXED: Domain folders for features, shared folders for infrastructure

### Project Vault Structure (If Using Obsidian)

```
my-project/
├── .obsidian/              # Project vault (local to codebase)
├── A-project/              # Project-specific docs
│   ├── index.md           # "What Claude needs to know about this project"
│   ├── architecture.md
│   ├── api-reference.md
│   └── decisions/         # YYYY-MM-DD-topic.md format
├── B-sessions/             # Session logs (ephemeral, stays in project)
│   └── 2026-06-26-feature-x.md
├── CodeCompass.md         # This file — copied from Cephalon vault
├── Z-harvest/              # Generated at project end → sent to Cephalon
│   ├── lessons-learned.md
│   └── techniques-invented.md
└── src/
```

**Naming convention:** A-Z folders (not 00-99). A = active/core, Z = end-of-project output.

---

## Dependency Map

### External Services

| Service | Purpose | Health check | Fallback |
|---------|---------|--------------|----------|
| Postgres | Primary DB | `GET /health/db` | Read replica |
| Stripe | Payments | Dashboard status | Queue + retry |
| Redis | Cache | `redis-cli ping` | Direct DB query |

### Critical Libraries

<!-- These are NOT optional. Understand them before touching. -->

| Library | Used for | Docs link |
|---------|----------|-----------|
| Prisma | ORM | https://pris.ly/d/docs |
| Zod | Schema validation | https://zod.dev |

---

## Testing & Debugging

### Test Strategy

**Framework:** <!-- Jest, pytest, etc. -->

| Test type | Location | How to run | Coverage goal |
|-----------|----------|------------|---------------|
| Unit | `tests/unit/` | `npm test` | 80%+ |
| Integration | `tests/integration/` | `npm test:integration` | Critical paths |
| E2E | `tests/e2e/` | `npm test:e2e` | Happy paths |
| **Adversarial** | `tests/adversarial/` | `npm test:adversarial` | Edge cases |

### Adversarial Testing with Free Models

**Concept:** Use free OpenRouter models as infinite "malicious user" simulators to find edge cases before real users do.

**Why it works:**
- Free models excel at creative breaking (exactly what we need)
- $0 cost = thousands of adversarial attempts
- Hallucinations become malicious payloads — a feature, not a bug
- No trust required — findings are pure profit

**Quick implementation:**

```python
def adversarial_test(input_schema, your_handler, n_attempts=50):
    """Use free LLMs to simulate malicious users breaking your code."""
    prompt = f"Break this input field: {json.dumps(input_schema)}. Return ONLY raw input."
    for i in range(n_attempts):
        malicious_input = call_openrouter_free(prompt)
        try:
            your_handler(malicious_input)
        except Exception as e:
            print(f"FOUND BUG: {malicious_input} → {e}")
            return malicious_input, e
    return None, "No bugs found"
```

**Best free coding models (OpenRouter):**
- `poolside/la-m1` — Flagship coding agent, 256K context
- `cohere/north-mini-code` — Agentic coding, 256K context
- `google/gemma-4-31b` — Strong coding + reasoning
- `nvidia/nemotron-3-ultra` — 1M context, orchestration
- `openrouter/free` — Auto-router (random free models)

**When to use:** Input validation fuzzing, API testing, form boundaries, edge case discovery.

**When NOT to use:** Correctness verification (use real tests), performance benchmarking, security audits.

**See also:** `[[adversarial-testing-free-models]]` in Cephalon vault.

### Common Debug Commands

<!-- Copy-paste these. Keep them working. -->

```bash
# Check service health
curl http://localhost:3000/health

# Tail error logs
journalctl -u myapp -f

# Run specific test
npm test -- --grep "should create user"

# Database connection test
psql $DATABASE_URL -c "SELECT 1"
```

### Error Patterns

| Error | Likely cause | Fix location |
|-------|--------------|--------------|
| ECONNREFUSED postgres | DB not running | `docker-compose up db` |
| JWT_INVALID_SIGNATURE | Secret mismatch | `.env` check |
| <!-- Add more --> | | |

---

## Environment Setup

### Local Development

```bash
# One-time setup
cp .env.example .env
docker-compose up -d
npm install
npm run db:migrate

# Start dev server
npm run dev
```

### Environment Differences

| Variable | Local | Staging | Production |
|----------|-------|---------|------------|
| `NODE_ENV` | development | staging | production |
| `LOG_LEVEL` | debug | info | warn |
| `RATE_LIMIT` | off | 100/min | 1000/min |

---

## Deployment & Git

### Deployment Workflow

1. Branch from `main` → `feature/what-it-does`
2. Push → PR created automatically
3. Tests run → must pass
4. Manual review required for prod changes
5. Merge → deploys to staging
6. Promote to prod: `./deploy.sh prod <commit>`

### Rollback Procedure

```bash
# Last good commit
git revert HEAD
# Or deploy previous version
./deploy.sh prod <previous-commit>
```

### Commit Convention

```
feat: add payment webhook handler
fix: handle timeout in stripe client
docs: update deployment guide
chore: upgrade dependencies
```

---

## Performance & Security

### Known Hotspots

| Area | Why it's slow | Mitigation |
|------|---------------|------------|
| Report generation | O(n²) loop | Cached for 5min |
| User export | Large CSV streaming | Moved to background job |

### Security Boundaries

| Boundary | Mechanism | What it protects |
|----------|-----------|------------------|
| Auth | JWT middleware | All /api/* routes |
| Admin | RBAC checks | Admin-only routes |
| Rate limit | Redis-based | Public endpoints |

**Sensitive data handling:**
- Never log `password`, `token`, `secret`
- PII in DB only, never in logs
- Use env vars for secrets, never commit

---

## Technical Debt

| Shortcut | Why it exists | Upgrade path |
|----------|---------------|--------------|
| Global lock on cache | Simple, works | Per-account locks if throughput matters |
| O(n²) filter | N < 1000 always | Revisit when N > 5000 |

<!-- Tag with: ponytail: [description] // ponytail: [ceiling], [upgrade path] -->

---

## Common Gotchas & Patterns

### Testing Gotchas

**Vitest v4+ Config Requirement:**
- **Symptom:** Tests fail with "test is not a function"
- **Fix:** Create separate `vitest.config.ts` (not embedded in `vite.config.ts`)
- **Why:** Vitest v4+ requires separate config file

### TypeScript Gotchas

**Type-Only Imports:**
- **Symptom:** "Import unused" errors for types
- **Fix:** Use `import type { TypeName }` for type-only imports
- **Why:** TypeScript 5+ enforces stricter type vs value import distinction

### Mobile Deployment Gotchas

**Capacitor Android Studio Version:**
- **Gotcha:** Flathub provides very recent versions (e.g., 2026.1.1.10)
- **Impact:** No issues, but version mismatch vs documentation
- **Note:** Capacitor works with recent versions; no fix needed

### Local LLM Experiments

**Current Status (2026-06-27):**
- **Tried:** Gemma4:12b for development
- **Result:** Excessive hallucination, poor pattern recognition, no project context
- **Verdict:** Non-viable for development; use cloud assistants (Claude, Copilot)
- **Future:** Revisit with better models (CodeQwen, DeepSeek Coder v2) + ponytail audit

**Key requirement:** Ponytail skill audit at every turn, regardless of code source (human, cloud, local).

### Development Patterns

**Mobile-First UX Patterns:**
- Safe-area insets: `env(safe-area-inset-*)` for notched devices
- Touch targets: ≥44px for interactive elements
- App-like feel: `-webkit-tap-highlight-color: transparent`

**Controlled Components Pattern (React):**
- Form state in parent component (single source of truth)
- Controlled inputs with `useState`
- Validation at trust boundary (business logic throws on invalid input)

**Thai Localization Pattern:**
- HTML: `lang="th"` attribute
- Currency: `Intl.NumberFormat('th-TH', { style: 'currency', currency: 'THB' })`
- Fonts: `'Noto Sans Thai', sans-serif` fallback

---

## Skills & Tools (When to Use What)

### Superpowers Skills

<!-- These change how you work. Check BEFORE starting. -->

| Skill | What it does | When to use |
|-------|--------------|-------------|
| **ponytail** | Enforces lazy coding — shortest working solution | ALL coding. Default: full |
| **brainstorming** | Quick idea generation before implementing | "How should I build X?" |
| **test-driven-development** | Red-green-refactor discipline | Bug fixes, complex logic |
| **systematic-debugging** | Structured approach to finding root cause | Any bug. Don't guess. |
| **code-review** | Automated review for bugs & cleanups | After completing work |
| **using-git-worktrees** | Parallel feature branches | Multiple features at once |

### Ponytail Embed Pattern (CRITICAL)

**ALWAYS embed ponytail in project CLAUDE.md:**

```markdown
## Ponytail Protocol

This project uses the ponytail skill to prevent over-engineering.

**Before ANY code change:**
1. Invoke the ponytail skill to assess the change
2. Climb the ladder: YAGNI → reuse → stdlib → native → one-line → write minimum
3. Mark deliberate shortcuts with `// ponytail:` comments
4. Apply to ALL code, regardless of source (human, cloud assistant, local model)

**Usage:** `/ponytail` or invoke via Skill tool when making changes.
```

**Why:** Source-agnostic quality control. Ponytail audits human code, cloud assistants, AND local LLM experiments equally.

### How Ponytail Works

**The ladder** — stop at first rung that holds:
1. Does this need to exist at all? (YAGNI)
2. Already in this codebase? Reuse it
3. Stdlib does it? Use stdlib
4. Native platform feature covers it? (CSS over JS)
5. Already-installed dependency solves it? Use it
6. Can it be one line? One line
7. Only then: write the minimum code that works

**Mark shortcuts:** `// ponytail: global lock, upgrade to per-account if throughput matters`

### When NOT to be Lazy

Never skip:
- Input validation at trust boundaries
- Error handling that prevents data loss
- Security measures
- Accessibility basics
- Anything explicitly requested

### Other Helpful Skills

| Skill | When to use |
|-------|-------------|
| **verify** | After implementation — run the app and confirm it works |
| **run** | Launch project app (auto-detects project type) |
| **code-review** | Review diff for bugs & simplifications |
| **simplify** | Review-only: suggest cleanups, no bug focus |
| **frontend-design** | UI/UX implementation guidance |

---

## Model Selection Strategy

**High-Performance Models (Fable 5, Opus 4.8):** Use for orchestration, architecture, decisions.
**Low-Performance Models (Haiku 4.5, Sonnet 4.6):** Use for grunt work: boilerplate, tests, docs.

### Task Model Tiering

Each task should be marked by model tier:
- **HIGH**: Architecture decisions, complex refactors, security reviews, performance optimization
- **MEDIUM**: Feature implementation, bug fixes, code reviews, API design
- **LOW**: Boilerplate generation, test writing, documentation, formatting

### Workflow

1. **Project orchestration** → Use high-performance model (Fable/Opus)
2. **Delegated tasks** → Match model to task complexity
3. **Grunt work** → Use low-performance model (Haiku/Sonnet) to save tokens

**Reminder:** When spawning agents or subagents, specify appropriate model tier for their task.

### Antigravity Tiered Workflow (Rate Limit Management)

**For large projects with rate limits:**

**Phase 1: Planning (High-Performance)**
- Vault Claude → Project scaffolding and prep work
- Project Claude → Architecture and detailed planning
- **Model:** High-performance (worth the cost for good planning)

**Phase 2: Implementation (High-Performance)**
- Antigravity IDE → Feature implementation with high-performance Claude
- Ponytail auditing → Code quality enforcement
- **Model:** High-performance (rapid iteration, complex tasks)

**Phase 3: Fallback (Low-Performance)**
- Low-performance model (GLM 4.7) → Routine work, documentation, simple features
- **Trigger:** Rate limits exhausted
- **Cycle back:** To Phase 2 for critical implementation phases

**When to use:** Large multi-day projects, rate-limited models, mixed complexity tasks

**Proven results:** Thai Mortgage Calculator JP language switcher added in minutes via Antigravity after exhausting GLM 4.7 limits.

---

## Session Workflow

### Start of Session

1. **Read this file first** (CodeCompass.md)
2. **Check hot.md** if using vault memory
3. **Activate relevant skills** before starting work
4. **Create task list** if multi-step work
5. **Choose appropriate model tier** for each task

### During Work

- Update sections when architecture changes
- Link to external docs, don't duplicate
- Commit frequently with meaningful messages

### End of Session

- Update "Current State" section
- Add new technical debt to debt table
- Note any new error patterns discovered
- Update "Last deployed" if shipped

### End of Project: Harvest Report

When project completes, produce a **structured harvest report** (use template from `~/Cephalon/90-templates/project-vault/Z-harvest/`).

**Sections to include:**
1. **Lessons Learned** — What worked, what failed, gotchas discovered
2. **Techniques Invented** — Novel approaches worth reusing (link to code examples)
3. **Architecture Decisions** — Key choices with rationale
4. **Technical Debt Created** — Shortcut catalog with upgrade paths
5. **Patterns to Share** — Reusable abstractions, conventions that worked

**Format:** Markdown with wiki-links to relevant files. Example:
```markdown
## Technique: PostgreSQL Connection Pooling

**Problem:** Connection exhaustion in serverless functions
**Solution:** PgBouncer with transaction pooling mode
**Code:** `src/db/pool.ts` lines 12-45
**See also:** `[[Cephalon:database-connection-pooling]]` (to be created in vault)
```

## Vault Integration: Consume and Discard

**For Vault Claude (launched from ~/Cephalon):**

When you receive a project harvest report:
1. **Study the report** thoroughly - understand context, techniques, decisions
2. **Extract useful bits** into appropriate vault locations:
   - Techniques → `10-knowledge/` (new reference files)
   - Decisions → `30-decisions/` (format: `YYYY-MM-DD-topic.md`)
   - Lessons → integrate into existing knowledge
   - Patterns → add to relevant sections
3. **Cross-reference** new vault entries to project codebase where useful
4. **DELETE the harvest report** - do not keep raw project reports in vault

**Rationale:** Keep vault lean. Store distilled knowledge, not raw project dumps. The project vault stays with the codebase; the central vault holds reusable patterns.

**Workflow:**
```bash
# Project Claude (in project dir)
cd ~/projects/my-app
# Produce harvest report → save to Z-harvest/

# Vault Claude (in Cephalon)
cd ~/Cephalon
# Paste harvest report → integrate → delete report
```

---

## Missing?

If you find yourself repeatedly explaining the same thing:
1. Add it to the appropriate section above
2. Keep it concise — one line per item when possible
3. Link to external docs for details

---

## FAQ (Quick Answers)

<!-- Add Q&A for things Claude keeps getting wrong -->

**Q: How do I add a new API endpoint?**
A: Route in `src/routes/`, handler in `src/handlers/`, validation in `src/schemas/`. Tests in `tests/integration/`.

**Q: Where do I put background jobs?**
A: `src/jobs/` — each job is a file with `run()` function. Worker picks up automatically.

**Q: Which model should I use for this task?**
A: HIGH for architecture/decisions, MEDIUM for implementation/bugs, LOW for boilerplate/docs. Use Antigravity tiered workflow if rate limits are a concern.

**Q: Should I use ponytail for this change?**
A: YES. Ponytail applies to ALL code changes, regardless of source (human, cloud, local model). Embed in project CLAUDE.md.

**Q: Local LLM or cloud assistant?**
A: Cloud assistants (Claude, Copilot) for development. Local LLMs (Gemma4:12b) currently non-viable due to hallucination. Revisit with better models + ponytail audit.

**Q: Should I use adversarial testing?**
A: YES for edge case discovery on user-facing inputs (forms, APIs, validation). NO for correctness verification or security audits. Use free OpenRouter models as malicious user simulators.

<!-- Add more as needed -->

---

**Version:** 2026-06-28 (Added adversarial testing with free models approach)
**Maintainer:** <!-- Who owns this doc -->
**Source:** Cephalon vault (`~/Cephalon/CodeCompass.md`) — Copy to project root and customize
