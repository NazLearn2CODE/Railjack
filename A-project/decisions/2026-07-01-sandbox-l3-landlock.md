---
date: 2026-07-01
status: proposed
---

# ADR: Security Layer 3 — Landlock write-confinement (self-sandbox at startup)

## Context

The blueprint (`[[agentic-os-guide]]` §2.2) specifies four Linux security layers. L1
(workspace boundary), L2 (catastrophic-shell blocklist), and L4 (HMAC tool receipts) are
enforced at the PreToolUse hook — see `[[2026-07-01-security-l1-l2-l4]]`. **L3 (OS sandbox)
is the one remaining layer.**

L1/L2/approval protect against the *common* failure modes, but they share a weakness: they
all gate **tools** at the *application* layer. If that layer is bypassed — prompt injection
that talks the model past the approval card, an operator approving out of habit, or a
dangerous command that falls through L2's denylist as an "exotic form" (e.g. `rm -r -f /`
with split flags) — nothing kernel-level stops a write to `~/.bashrc`, `/etc/hosts`, or an
arbitrary directory. L3 is the machine-enforced backstop: **contain writes at the kernel,
so the application layer failing is not catastrophic.**

This ADR was designed through the brainstorming skill; three decisions were locked first:

1. **Threat model: write-corruption only.** Confine filesystem *writes*; leave reads open.
   The SDK, TLS, and NSS read a large, hard-to-enumerate set of paths at runtime; confining
   reads is brittle (a missed path = a crash deep in the agent loop). Read-confine­ment
   (e.g. blocking `cat ~/.ssh/id_rsa`) is an **egress / L4 concern**, not L3's job. L3 stops
   *corruption*, not *exfiltration*.
2. **Attach model: self-Landlock at startup.** `restrict_self()` once, before any agent
   runs. Landlock restrictions are inherited across `fork`+`execve`, so the claude-agent-sdk
   CLI **and** its native Bash subprocess inherit the confinement — one call contains the
   whole process tree. This is the only attach model that actually reaches the SDK's internal
   subprocess: `ClaudeAgentOptions` exposes no pre-exec hook, so per-command `bwrap`
   wrapping of the SDK's own spawn is not possible without re-spawning the SDK and fighting
   its abstraction.
3. **No-sandbox policy: fail-open + warn.** If Landlock is unavailable (`EPERM`/`ENOSYS`/
   `EOPNOTSUPP`, a non-Linux dev box, or a locked-down kernel), Orbiter still starts, logs a
   prominent `WARNING`, reports `sandbox=none` on `/api/health`, and falls back to
   L1/L2/approval. A local single-user OS must not refuse to run because the kernel won't
   cooperate; the backstop degrades to the existing application-layer gate.

## Decision

Add `app/core/sandbox.py` (stdlib only — `ctypes`, no new dependency) and self-sandbox
Orbiter once at startup.

### What L3 confines

The Landlock ruleset **handles write-type accesses only** and then restricts them to a
small allowlist. After `restrict_self()`, those accesses are **denied everywhere except**
the allowlisted roots; reads and execute everywhere remain allowed.

Handled (confined) access types:

```
WRITE_FILE | REMOVE_DIR | REMOVE_FILE
| MAKE_CHAR | MAKE_DIR | MAKE_REG | MAKE_SOCK | MAKE_FIFO | MAKE_BLOCK | MAKE_SYM
| (REFER | TRUNCATE)   # ABI v2 (kernel 5.19+) only
```

Deliberately **not handled**: `EXECUTE`, `READ_FILE`, `READ_DIR` (reads stay open — see
threat model). Network access (`LANDLOCK_ACCESS_NET_*`, ABI v4 / kernel 6.7+) is **not**
handled in v1 — see Deferred.

### Write allowlist (the only writable roots after lockdown)

- **Workspace root** — `ORBITER_WORKSPACE_ROOT` (default: project root). The agent's job.
- **`/tmp`** — plus `$TMPDIR` if set. SDK temp files.
- **`~/.claude`** — SDK session state (transcripts, todos, `projects/` cache). Probed
  `~/.claude` exists at runtime on the dev box; the SDK writes session state there.

Anything else (home dotfiles, `/etc`, other project dirs, `~/.ssh`, device nodes) is
**write-denied at the kernel** regardless of what the agent, the approval card, or L2 says.

### Component: `app/core/sandbox.py`

```
class Sandbox(Protocol):
    def apply(self) -> SandboxStatus: ...

@dataclass(frozen=True)
class SandboxStatus:
    active: bool
    mechanism: str          # "landlock" | "none"
    abi: int | None         # Landlock ABI version if probed
    writable_roots: list[str]
    reason: str             # "" when active; the failure reason when not

class LandlockSandbox(Sandbox):
    def __init__(self, writable_roots: list[Path]): ...
    def _probe_abi(self) -> int:
        # syscall(444, NULL, 0, 0) → ABI version (≥0), or raise on EPERM/ENOSYS/EOPNOTSUPP.
    def apply(self) -> SandboxStatus: ...

class NoopSandbox(Sandbox):
    # apply() returns inactive(mechanism="none"). Used when ORBITER_SANDBOX=none.
```

`LandlockSandbox.apply()` flow:

1. **Probe ABI** via `landlock_create_ruleset(NULL, 0, 0)` (syscall 444). `<0` ⇒ return
   inactive status with the errno reason (fail-open). Do **not** call `restrict_self`.
2. **Mask the handled write-bits to the ABI.** ABI v1 supports fs bits 0–12 (`0x1FFF`);
   ABI ≥2 adds `REFER`/`TRUNCATE` (`0x7FFF`). Passing v2 bits on a v1 kernel returns
   `EINVAL` from `create_ruleset`, so the mask is computed from the probed ABI.
3. **Build the ruleset.** `struct landlock_ruleset_attr{ handled_access_fs }` (one `__u64`).
   `create_ruleset(&attr, sizeof(attr), 0)` (444) → ruleset fd.
4. **Add one rule per writable root** via `landlock_add_rule(ruleset_fd,
   LANDLOCK_RULE_PATH_BENEATH, &path_beneath_attr{allowed_access=handled, parent_fd}, 0)`
   (445), with `parent_fd` opened `O_PATH | O_CLOEXEC` on each root.
5. **Restrict self.** `landlock_restrict_self(ruleset_fd, 0)` (446). Close the fd
   (restrictions persist independent of the fd after `restrict_self`).
6. Return active status with the resolved roots.

All three syscalls are invoked through `libc.syscall(NR, …)` by raw syscall number
(`444`/`445`/`446` on x86_64), because this box's glibc does not expose the
`landlock_*` symbols (confirmed). Structs are hand-built with `ctypes.Structure`.

### Wiring: `app/main.py`

- Startup (FastAPI lifespan / `@app.on_event("startup")`): construct
  `LandlockSandbox(writable_roots=[workspace, tmp, ~/.claude] + extra)`, call `.apply()`,
  store the `SandboxStatus` on the app state.
- `GET /api/health` returns `sandbox: {active, mechanism, abi, writable_roots}` alongside
  the existing `status: OK`.
- Env:
  - `ORBITER_SANDBOX=landlock|none` (default `landlock`). `none` ⇒ `NoopSandbox`.
  - `ORBITER_SANDBOX_EXTRA_ROOTS` — colon-separated extra writable roots (e.g. a logs dir
    the operator keeps outside the workspace).
- Logging: `WARNING` when inactive (fail-open — loud, because the backstop is down);
  `INFO` with the resolved writable roots when active.

### L4 (receipts) — no change in v1

Landlock is a kernel layer, orthogonal to the PreToolUse receipt path. v1 makes no L4
change. (A later iteration may snapshot `sandbox_status` into each receipt — deferred; YAGNI.)

## Config

- `ORBITER_SANDBOX` — `landlock` (default) | `none`.
- `ORBITER_SANDBOX_EXTRA_ROOTS` — `:`-separated extra writable roots.
- `ORBITER_WORKSPACE_ROOT` — workspace root (existing; reused from L1).

## What's deferred

- **bwrap / podman fallback + re-exec.** If `restrict_self` is unavailable, Orbiter *could*
  re-exec itself under `bwrap` for namespace isolation. Real complexity (argv/env
  reconstruction, mount table), likely overkill until Landlock proves insufficient in
  practice. The graded fallback in the blueprint is satisfied by Landlock for v1.
- **Read confinement** (`READ_FILE`/`READ_DIR` handling) — brittle allowlist; out of scope
  per threat model.
- **Network egress allowlist** (`LANDLOCK_ACCESS_NET_*`, ABI v4 / kernel 6.7+) — would pin
  the LLM API host; stronger but needs endpoint pinning. Deferred.
- **`Sandbox` second implementation** — the `Protocol` exists for testability and the
  fallback, not as premature abstraction; bwrap/podman impls arrive with the re-exec work.
- **Per-agent (not process-wide) roots**, **dashboard viewer** for sandbox status,
  **receipt snapshot** of sandbox status.

## Reversible?

Yes — `ORBITER_SANDBOX=none` selects `NoopSandbox` and applies no restriction (reverts to
the pre-L3, application-layer-only posture). The startup hook is the single integration
point; removing it fully reverts L3. Landlock restrictions applied to a running process are
**irreversible within that process** (monotonic — you can only add, never relax), which is
exactly the property a backstop needs; restart to change posture.

## Impact

- **Kernel-enforced write containment**: an inattentive operator, a prompt-injected bypass,
  or an L2 near-miss (`rm -r -f ~` split flags) can no longer corrupt anything outside the
  allowlist. The application-layer gate failing is no longer catastrophic.
- **Reaches the SDK subprocess tree** via inheritance — the single hard-won property that
  makes L3 meaningful for a claude-agent-sdk host.
- **Observable**: `/api/health` tells the operator whether the backstop is actually up and
  exactly which roots are writable — no silent "I thought it was sandboxed."
- **Degrades safely**: on macOS, locked-down kernels, or a Claude-Code-sandboxed dev shell
  (where the probe returns `EPERM`), Orbiter still runs with the existing L1/L2/approval
  floor and a visible warning.

## Risk notes

- **Live validation gap.** `landlock_create_ruleset(NULL,0,0)` returns `EPERM` and `bwrap`
  fails to create namespaces **when run from inside Claude Code's own sandbox** (this dev
  session). That does **not** mean Landlock is unusable on the deployment target — the real
  Orbiter process (bare `uvicorn`) is not under that sandbox. The gated live self-check
  (below) is the validation; it must be run against a real Orbiter process, not from the
  Claude Code shell. Until that passes, "L3 works" is design-confirmed, not runtime-confirmed.
- **Allowlist under-coverage surfaces as tool errors, not silent success.** If the SDK
  writes to a path we didn't allowlist, that write gets `EACCES` and surfaces as a tool
  error to the agent/operator — a visible, fixable failure (add the root via
  `ORBITER_SANDBOX_EXTRA_ROOTS`), never silent corruption. The three allowlisted roots are
  the known SDK footprint; misses are expected to surface early.
- **Monotonic / coarse.** Self-Landlock confines the *whole* Orbiter process, not per-agent.
  All agents share the same writable roots in v1. Per-agent roots are deferred.
- **ABI dependence.** The mask logic assumes ABI v1 (bits 0–12) vs ≥v2 (+`REFER`/`TRUNCATE`).
  A future ABI adding new write-type fs bits would not be handled until the mask is updated —
  safe-by-omission (unhandled = unconfined for that bit), not unsafe.

## Testing

- **Unit (kernel-free, run anywhere incl. CI / Claude-Code-sandboxed):**
  - `_probe_abi` result → expected ABI mapping.
  - Write-bit mask vs ABI: ABI 1 ⇒ `0x1FFF`-bounded; ABI ≥2 ⇒ `0x7FFF`.
  - Allowlist path normalization (`~` expansion, absolutization, dedup).
  - `NoopSandbox.apply()` returns the documented inactive shape.
  These never call `restrict_self`, so they are safe under any sandbox.
- **Live self-check** (gated behind `ORBITER_SANDBOX_LIVE=1`, off by default): actually
  restricts a child process, then asserts a write to an allowlisted root succeeds and a
  write to a denied path (`~/orbiter_l3_probe_deny`) raises `PermissionError`. The runnable
  check non-trivial logic demands — gated so it does not lock down the dev process or every
  test run. Must be run against a real Orbiter process, not the Claude Code shell (see Risk).
