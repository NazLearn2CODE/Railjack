# L3 Landlock Sandbox — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add blueprint §2.2 Layer 3 — a kernel-enforced (Landlock) write-confinement backstop that self-sandboxes Orbiter at startup so the agent and its inherited SDK/Bash subprocess tree cannot corrupt the filesystem outside a small allowlist.

**Architecture:** `restrict_self()` once at startup via raw Landlock syscalls (ctypes, no dep). The ruleset handles write-type accesses only; reads/exec stay open. Restrictions inherit across fork+exec, reaching the claude-agent-sdk CLI and its native Bash. Fail-open if Landlock is unavailable. Spec: `A-project/decisions/2026-07-01-sandbox-l3-landlock.md`.

**Tech Stack:** Python 3.14, stdlib `ctypes`/`os`/`errno`, FastAPI (wiring), pytest.

**Key correctness points (apply throughout Task 4):**
- Pass every `libc.syscall` arg **explicitly typed** (`ctypes.c_long`/`c_int`/`c_size_t`/`c_uint32`/`byref`). ctypes defaults args to `c_int` → silently truncates 64-bit bitmasks/pointers.
- Load libc with `use_errno=True`; read failures via `ctypes.get_errno()` after a negative return.
- Mask handled write-bits to the probed ABI (v1 = `0x1FFF`; ≥v2 = `0x7FFF` incl. `REFER`/`TRUNCATE`).
- Ensure the three standard roots exist (`mkdir(exist_ok=True)`) before `os.open(O_PATH)` — `~/.claude` may not yet exist at startup.

---

## Chunk 1: Pure foundation (kernel-free, fully TDD)

### Task 1: `SandboxStatus`, `Sandbox` Protocol, `NoopSandbox`

**Files:**
- Create: `app/core/sandbox.py`
- Test: `tests/test_sandbox.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sandbox.py
from app.core.sandbox import NoopSandbox, SandboxStatus


def test_noop_sandbox_returns_inactive_status():
    status = NoopSandbox().apply()
    assert isinstance(status, SandboxStatus)
    assert status.active is False
    assert status.mechanism == "none"
    assert status.reason  # non-empty
    assert status.abi is None
    assert status.writable_roots == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sandbox.py::test_noop_sandbox_returns_inactive_status -v`
Expected: FAIL — `ModuleNotFoundError: app.core.sandbox`

- [ ] **Step 3: Write minimal implementation**

```python
# app/core/sandbox.py
"""Blueprint §2.2 Layer 3 — Landlock write-confinement (self-sandbox at startup).

Restricts the Orbiter process (and its inherited subprocess tree: the claude-agent-sdk
CLI + its native Bash) to a small set of writable roots. WRITE-type accesses are
confined; reads/execute everywhere stay open.

Fail-open: if Landlock is unavailable (EPERM/ENOSYS/EOPNOTSUPP, non-Linux, locked-down
kernel, or a sandboxed dev shell), apply() returns inactive and Orbiter continues on the
existing L1/L2/approval floor. See ADR 2026-07-01-sandbox-l3-landlock.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger("orbiter.sandbox")


@dataclass(frozen=True)
class SandboxStatus:
    active: bool
    mechanism: str  # "landlock" | "none"
    abi: int | None = None
    writable_roots: list[str] = field(default_factory=list)
    reason: str = ""


class Sandbox(Protocol):
    def apply(self) -> SandboxStatus: ...


class NoopSandbox:
    """No confinement. Used when ORBITER_SANDBOX=none."""
    def apply(self) -> SandboxStatus:
        logger.warning("Sandbox DISABLED (NoopSandbox) — running on L1/L2/approval only")
        return SandboxStatus(active=False, mechanism="none", reason="disabled by config")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_sandbox.py::test_noop_sandbox_returns_inactive_status -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/sandbox.py tests/test_sandbox.py
git commit -m "feat(security): L3 sandbox scaffold — Sandbox protocol + NoopSandbox"
```

---

### Task 2: `write_mask_for_abi(abi)` — pure ABI→access-bit mapping

**Files:**
- Modify: `app/core/sandbox.py` (add constants + function)
- Test: `tests/test_sandbox.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_sandbox.py
from app.core.sandbox import write_mask_for_abi


def test_write_mask_abi_v1_excludes_refer_truncate():
    # ABI 1: write bits within 0x1FFF; REFER (13) / TRUNCATE (14) NOT set.
    mask = write_mask_for_abi(1)
    assert mask & 0x1FFF == mask  # no bits above the v1 range
    assert mask & (1 << 13) == 0  # REFER absent
    assert mask & (1 << 14) == 0  # TRUNCATE absent
    assert mask & (1 << 1) != 0   # WRITE_FILE present


def test_write_mask_abi_v2_includes_refer_truncate():
    # ABI >=2: adds REFER + TRUNCATE.
    mask = write_mask_for_abi(2)
    assert mask & (1 << 13) != 0  # REFER present
    assert mask & (1 << 14) != 0  # TRUNCATE present
    assert mask & 0x7FFF == mask  # within v2 range
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sandbox.py -k write_mask -v`
Expected: FAIL — `ImportError: cannot import name 'write_mask_for_abi'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to app/core/sandbox.py (after the imports / before SandboxStatus is fine)
# Landlock filesystem access flags (linux/landlock.h). We HANDLE (confine) writes only.
_FS_WRITE_FILE  = 1 << 1
_FS_REMOVE_DIR  = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_CHAR   = 1 << 6
_FS_MAKE_DIR    = 1 << 7
_FS_MAKE_REG    = 1 << 8
_FS_MAKE_SOCK   = 1 << 9
_FS_MAKE_FIFO   = 1 << 10
_FS_MAKE_BLOCK  = 1 << 11
_FS_MAKE_SYM    = 1 << 12
_FS_REFER       = 1 << 13   # ABI v2 (kernel 5.19+)
_FS_TRUNCATE    = 1 << 14   # ABI v2

# Write accesses confined in v1. Reads (READ_FILE/READ_DIR) + EXECUTE stay open everywhere.
_WRITE_ACCESSES = (
    _FS_WRITE_FILE | _FS_REMOVE_DIR | _FS_REMOVE_FILE
    | _FS_MAKE_CHAR | _FS_MAKE_DIR | _FS_MAKE_REG | _FS_MAKE_SOCK
    | _FS_MAKE_FIFO | _FS_MAKE_BLOCK | _FS_MAKE_SYM
)
_ABI1_FS_MASK = 0x1FFF  # ABI v1 supports fs bits 0–12
_ABI2_FS_MASK = 0x7FFF  # ABI v2 adds REFER (13) + TRUNCATE (14)


def write_mask_for_abi(abi: int) -> int:
    """Confined write-access bitmask for a given Landlock ABI version.

    Passing v2 bits on a v1 kernel makes create_ruleset return EINVAL, so mask by ABI.
    """
    if abi <= 1:
        return _WRITE_ACCESSES & _ABI1_FS_MASK
    return (_WRITE_ACCESSES | _FS_REFER | _FS_TRUNCATE) & _ABI2_FS_MASK
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_sandbox.py -k write_mask -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add app/core/sandbox.py tests/test_sandbox.py
git commit -m "feat(security): L3 — Landlock write-bit ABI masking"
```

---

### Task 3: `normalize_roots(roots, extra)` — pure path normalization

**Files:**
- Modify: `app/core/sandbox.py`
- Test: `tests/test_sandbox.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_sandbox.py
from pathlib import Path
from app.core.sandbox import normalize_roots


def test_normalize_roots_expands_and_dedups_preserving_order():
    roots = [Path("~/foo"), Path("/tmp"), Path("~/foo/bar")]
    # extra duplicates /tmp and adds one new
    out = normalize_roots(roots, extra="/tmp:/opt/orbiter")
    assert out[0].endswith("/foo")
    assert out[1] == "/tmp"
    assert out[-1] == "/opt/orbiter"
    assert out.count("/tmp") == 1          # dedup
    assert all(p.startswith("/") for p in out)  # absolutized
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sandbox.py::test_normalize_roots_expands_and_dedups_preserving_order -v`
Expected: FAIL — `ImportError: cannot import name 'normalize_roots'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to app/core/sandbox.py
import os
from pathlib import Path


def normalize_roots(roots: list[Path], extra: str | None = None) -> list[str]:
    """Expand ~, absolutize, dedup (preserve order). Returns resolved path strings."""
    seen: set[str] = set()
    out: list[str] = []
    sources = list(roots)
    if extra:
        sources += [Path(p) for p in extra.split(":") if p.strip()]
    for r in sources:
        s = str(Path(os.path.expanduser(str(r))).resolve(strict=False))
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_sandbox.py::test_normalize_roots_expands_and_dedups_preserving_order -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/sandbox.py tests/test_sandbox.py
git commit -m "feat(security): L3 — writable-root normalization"
```

---

## Chunk 2: Landlock syscall integration

### Task 4: `LandlockSandbox.apply()` + gated live self-check

This is the syscall glue. It **cannot be unit-tested from the Claude Code sandbox** (Landlock probes `EPERM` there), so the only runnable check is the gated live self-check, run against a real Orbiter / bare Python process. The pure pieces it depends on (ABI mask, root normalization) are already covered by Tasks 2–3.

**Files:**
- Modify: `app/core/sandbox.py` (add ctypes structs, `_probe_abi`, `LandlockSandbox`, `_LandlockUnavailable`)
- Test: `tests/test_sandbox.py` (add gated live check)

- [ ] **Step 1: Write the gated live self-check**

```python
# append to tests/test_sandbox.py
import os, tempfile, pytest
from pathlib import Path
from app.core.sandbox import LandlockSandbox

LIVE = os.environ.get("ORBITER_SANDBOX_LIVE") == "1"


@pytest.mark.skipif(not LIVE, reason="gated: locks down the process; run vs a real Orbiter process")
def test_landlock_live_write_confinement():
    # Allowed root is a temp dir we can write; denied path is under HOME (outside allowlist).
    allowed = Path(tempfile.mkdtemp(prefix="orbiter_l3_allow_"))
    denied = Path.home() / "orbiter_l3_probe_deny"
    if denied.exists():
        denied.unlink()

    status = LandlockSandbox(writable_roots=[allowed]).apply()
    assert status.active, f"sandbox not active: {status.reason}"

    # Write inside the allowed root → succeeds.
    (allowed / "ok.txt").write_text("ok")
    # Write outside the allowlist → PermissionError (kernel-blocked).
    with pytest.raises(PermissionError):
        denied.write_text("should be blocked")

    # Cleanup what we can (allowed root still writable).
    (allowed / "ok.txt").unlink(missing_ok=True)
```

- [ ] **Step 2: Run test to verify it is SKIPPED (ungated runs must not lock the process)**

Run: `.venv/bin/pytest tests/test_sandbox.py::test_landlock_live_write_confinement -v`
Expected: SKIP — `gated: locks down the process...`

- [ ] **Step 3: Write the implementation**

```python
# append to app/core/sandbox.py
import ctypes
import ctypes.util
import errno

# x86_64 syscall numbers (this box's glibc doesn't expose the landlock_* symbols).
_NR_CREATE_RULESET  = 444
_NR_ADD_RULE        = 445
_NR_RESTRICT_SELF   = 446
_RULE_PATH_BENEATH  = 1


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]


class _LandlockUnavailable(Exception):
    def __init__(self, errno_code: int):
        super().__init__(f"errno {errno_code}")
        self.errno = errno_code


def _load_libc() -> ctypes.CDLL:
    path = ctypes.util.find_library("c")
    if path is None:
        raise _LandlockUnavailable(errno.ENOENT)  # ponytail: treat as unavailable, not crash
    libc = ctypes.CDLL(path, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


class LandlockSandbox:
    """Self-Landlock write-confinement. Restrictions inherit to subprocesses."""

    def __init__(self, writable_roots: list[Path], extra_roots: str | None = None):
        self._roots = normalize_roots(writable_roots, extra_roots)

    def _probe_abi(self, libc: ctypes.CDLL) -> int:
        ret = libc.syscall(ctypes.c_long(_NR_CREATE_RULESET), None,
                           ctypes.c_size_t(0), ctypes.c_uint32(0))
        if ret < 0:
            raise _LandlockUnavailable(ctypes.get_errno())
        return int(ret)

    def apply(self) -> SandboxStatus:
        # 1. Probe + mask. Fail-open on any unavailability.
        try:
            libc = _load_libc()
            abi = self._probe_abi(libc)
        except _LandlockUnavailable as e:
            logger.warning("Landlock unavailable (errno %s=%s) — fail-open on L1/L2/approval",
                           e.errno, errno.errorcode.get(e.errno, "?"))
            return SandboxStatus(active=False, mechanism="landlock",
                                 reason=f"unavailable: errno {e.errno}")
        handled = write_mask_for_abi(abi)

        # 2. Build ruleset, add a rule per root, restrict self.
        ruleset_fd = -1
        try:
            attr = _RulesetAttr(handled_access_fs=handled)
            ruleset_fd = libc.syscall(ctypes.c_long(_NR_CREATE_RULESET), ctypes.byref(attr),
                                      ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
            if ruleset_fd < 0:
                raise _LandlockUnavailable(ctypes.get_errno())

            for root in self._roots:
                # Ensure the root exists so O_PATH succeeds (~/.claude may not yet).
                try:
                    Path(root).mkdir(parents=True, exist_ok=True)
                except OSError:
                    logger.warning("Landlock: cannot create root %s — skipping", root)
                    continue
                parent_fd = os.open(root, os.O_PATH | os.O_CLOEXEC)
                try:
                    pb = _PathBeneathAttr(allowed_access=handled, parent_fd=parent_fd)
                    rc = libc.syscall(ctypes.c_long(_NR_ADD_RULE), ctypes.c_int(ruleset_fd),
                                      ctypes.c_int(_RULE_PATH_BENEATH), ctypes.byref(pb),
                                      ctypes.c_uint32(0))
                    if rc < 0:
                        logger.warning("Landlock add_rule failed for %s (errno %s)",
                                       root, errno.errorcode.get(ctypes.get_errno(), "?"))
                finally:
                    os.close(parent_fd)

            rc = libc.syscall(ctypes.c_long(_NR_RESTRICT_SELF),
                              ctypes.c_int(ruleset_fd), ctypes.c_uint32(0))
            if rc < 0:
                raise _LandlockUnavailable(ctypes.get_errno())
        except _LandlockUnavailable as e:
            logger.warning("Landlock restrict failed (errno %s) — fail-open", e.errno)
            return SandboxStatus(active=False, mechanism="landlock", abi=abi,
                                 reason=f"restrict failed: errno {e.errno}")
        finally:
            if ruleset_fd >= 0:
                with __import__("contextlib").suppress(OSError):
                    os.close(ruleset_fd)  # restrictions persist after close

        logger.info("Landlock sandbox ACTIVE (ABI %s). Writable roots: %s", abi, self._roots)
        return SandboxStatus(active=True, mechanism="landlock", abi=abi,
                             writable_roots=list(self._roots))
```

> Note on the `__import__("contextlib")` line: avoids adding a top-of-file import churn in this snippet; the implementing agent should hoist it to a normal `from contextlib import suppress` import at the top of the file and use `suppress(OSError):`.

- [ ] **Step 4: Run the full suite ungated — live check must SKIP, rest must PASS**

Run: `.venv/bin/pytest tests/test_sandbox.py -v`
Expected: Tasks 1–3 tests PASS; `test_landlock_live_write_confinence` SKIP. No `PermissionError` leaks into the run (proves the gate holds).

- [ ] **Step 5: Run the gated live check against a REAL process (NOT the Claude Code shell)**

This step is a manual verification, not part of the automated suite. From a bare shell (the user's terminal, not this sandboxed one):

```bash
cd "/var/home/NAZ/Coding Projects/Orbiter"
ORBITER_SANDBOX_LIVE=1 .venv/bin/pytest tests/test_sandbox.py::test_landlock_live_write_confinement -v
```
Expected: PASS (sandbox active; allowed write succeeds; `~/orbiter_l3_probe_deny` write raises `PermissionError`). If it fails with `active=False`/EPERM, the environment can't run Landlock — the fail-open path is correct, L3 is simply inactive there (document and move on).

- [ ] **Step 6: Commit**

```bash
git add app/core/sandbox.py tests/test_sandbox.py
git commit -m "feat(security): L3 — LandlockSandbox self-restrict + gated live check"
```

---

## Chunk 3: Gateway wiring + docs

### Task 5: Wire sandbox into `app/main.py` startup + `/api/health`

**Files:**
- Modify: `app/main.py` (startup applies sandbox; `/api/health` exposes status; env config)
- Test: `tests/test_health_sandbox.py`

**Design for testability:** the sandbox is built at startup from `ORBITER_SANDBOX`. The health test sets `ORBITER_SANDBOX=none` so startup uses `NoopSandbox` — the test process is never locked down.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_health_sandbox.py
import importlib
from fastapi.testclient import TestClient


def test_health_reports_noop_sandbox_when_disabled(monkeypatch):
    monkeypatch.setenv("ORBITER_SANDBOX", "none")
    import app.main as main
    importlib.reload(main)  # pick up env at startup
    client = TestClient(main.app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "OK"
    sb = body["sandbox"]
    assert sb["mechanism"] == "none"
    assert sb["active"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_health_sandbox.py -v`
Expected: FAIL — `KeyError: 'sandbox'` (health doesn't return it yet)

- [ ] **Step 3: Modify `app/main.py`**

Add the import and a startup hook. Near the top imports:

```python
from app.core.sandbox import LandlockSandbox, NoopSandbox
```

After the `security = SecurityPolicy(...)` block and before/after `manager = ...`, add:

```python
# L3 OS sandbox (blueprint §2.2): self-Landlock write-confinement at startup.
# Fail-open: NoopSandbox when disabled OR if Landlock is unavailable on this host.
def _build_sandbox():
    if os.environ.get("ORBITER_SANDBOX", "landlock").lower() == "none":
        return NoopSandbox()
    return LandlockSandbox(
        writable_roots=[
            Path(os.environ.get("ORBITER_WORKSPACE_ROOT", PROJECT_ROOT)).resolve(strict=False),
            Path(os.environ.get("TMPDIR", "/tmp")),
            Path("~/.claude").expanduser(),
        ],
        extra_roots=os.environ.get("ORBITER_SANDBOX_EXTRA_ROOTS"),
    )

sandbox = _build_sandbox()
sandbox_status = sandbox.apply()
```

Add the status to `/api/health`:

```python
@app.get("/api/health")
async def health():
    return {
        "status": "OK",
        "sandbox": {
            "active": sandbox_status.active,
            "mechanism": sandbox_status.mechanism,
            "abi": sandbox_status.abi,
            "writable_roots": sandbox_status.writable_roots,
            "reason": sandbox_status.reason,
        },
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_health_sandbox.py -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite + ruff**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check app tests`
Expected: all PASS, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_health_sandbox.py
git commit -m "feat(security): L3 — apply sandbox at startup, expose on /api/health"
```

---

### Task 6: Update architecture + index, final verification

**Files:**
- Modify: `A-project/architecture.md` (L3 line DONE)
- Modify: `A-project/index.md` (current status)

- [ ] **Step 1: Update the security-layer diagram in `architecture.md`**

Change the L3 line from `DEFERRED` to `DONE (app/core/sandbox.py)` and note the self-Landlock mechanism.

- [ ] **Step 2: Update `A-project/index.md` current-status**

Move L3 from "Next stage" to the done list; note live validation caveat (gated check must pass against a real Orbiter process).

- [ ] **Step 3: Final full verification**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check app tests`
Expected: green + clean. Confirm the gated live check is SKIP in the default run.

- [ ] **Step 4: Commit**

```bash
git add A-project/architecture.md A-project/index.md
git commit -m "docs: L3 Landlock sandbox landed in architecture + index"
```

---

## Notes for the implementer

- **The gated live check is the only validation of the actual syscalls.** Everything else is pure logic. Do not treat "tests pass" as "L3 works" until Step 5 of Task 4 passes against a real process — the EPERM you'll see from the Claude Code shell is expected, not a bug.
- **Fail-open is load-bearing.** Any new failure path in `apply()` must return an inactive `SandboxStatus`, never raise. A broken sandbox must not block Orbiter startup.
- **No new dependencies.** stdlib `ctypes` only. Do not reach for `pylandlock`.
