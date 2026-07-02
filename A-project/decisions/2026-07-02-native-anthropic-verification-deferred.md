---
date: 2026-07-02
status: accepted
---
# ADR: Native Anthropic-API verification — deferred with recipe

## Context

The PreToolUse approval gate + security policy floor are proven end-to-end on the
**z.ai/GLM** backend only (the dashboard UI loop: composer → token stream →
completion, and approval card → APPROVE → gated Bash executes). `index.md` lists
"native Anthropic-API verification (approval gate proven on z.ai only)" as deferred.
This session attempted that verification and hit a hard environmental blocker.

## Finding

Backend selection is **env-only**: `ClaudeSdkProvider` builds `ClaudeAgentOptions`
with no hardcoded `base_url`/`api_key`, so `claude_agent_sdk.query` reads
`ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` from the
environment. Switching to native Anthropic is therefore a pure env swap — **no code
change**. The blocker is a credential: the only auth present (env *and* `.env`) is
the 49-char z.ai token (`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` = `fbb2c20…`),
with `ANTHROPIC_BASE_URL = https://api.z.ai/api/anthropic`. A native Anthropic key is
`sk-ant-api03-…` (~108 chars); **none exists**, and the z.ai token won't authenticate
against `api.anthropic.com`. The app reads `os.environ` directly (no `load_dotenv`),
so config reaches it via the launching shell/profile; inline env on the launch command
overrides cleanly.

## Decision

Defer native verification until a `sk-ant-` key is supplied, and record the exact
recipe so it is minutes of work, not an investigation. The gate code is untouched —
this ADR is a runbook, not a change.

## Recipe (run once a native key exists)

```bash
# 1. Boot the gateway against native Anthropic (override the z.ai env at launch).
#    env -u ANTHROPIC_AUTH_TOKEN clears the z.ai bearer so it can't shadow the key.
env -u ANTHROPIC_AUTH_TOKEN \
    ANTHROPIC_API_KEY=sk-ant-api03-… \
    ANTHROPIC_BASE_URL=https://api.anthropic.com \
    .venv/bin/uvicorn app.main:app --port 8000 &

# 2. Build the dashboard (build gates on tsc).
( cd web && npm run build )

# 3. HAPPY PATH — approval gate fires under native Anthropic:
#    open http://localhost:8000, TEAM or single mode, dispatch a prompt that
#    forces Bash, e.g. "run `uname -a` and report the kernel".
#    EXPECT: an approval card renders (approval_needed on the /ws stream);
#    click APPROVE → gated Bash executes; one HMAC receipt appended to
#    logs/receipts.jsonl; tool result streams back.

# 4. SECURITY-FLOOR PROBE — catastrophic command hard-denies PRE-approval:
#    dispatch "run: rm -rf /".
#    EXPECT: hard-deny BEFORE any approval card (the L2 ShellPolicy outranks
#    operator approval); no receipt for an allowed execution; agent told it was
#    blocked.
```

Programmatic equivalent of step 3 (no browser): `POST /api/sessions` → read the
`approval_needed` frame off `ws://localhost:8000/ws/sessions/{id}` →
`POST /api/sessions/{id}/approve {approval_id, approve:true}` → confirm the Bash
result frame + the `logs/receipts.jsonl` entry.

## Reversible?

N/A — documentation only; no code changed.

## Impact

- Closes the loop on "what's left for native verification": the gate code is
  backend-agnostic by construction (it's a PreToolUse hook, the path taken precisely
  because z.ai never invokes the SDK's `--permission-prompt-tool`); the only
  unverified leg is *running it* against real Claude, blocked solely on a key.
- A future session with a `sk-ant-` key finishes this in minutes by running the recipe
  and recording the observed frames as the verification evidence.

## Test surface

Deferred. The drive above IS the test surface; nothing is added to the SDK-free
backend suite (the gate's logic is already covered by the 57 existing tests; this
verifies the *integration*, not the logic).
