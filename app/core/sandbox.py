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
