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

# Landlock filesystem access flags (linux/landlock.h). We HANDLE (confine) writes only.
_FS_WRITE_FILE = 1 << 1
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_CHAR = 1 << 6
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_MAKE_SOCK = 1 << 9
_FS_MAKE_FIFO = 1 << 10
_FS_MAKE_BLOCK = 1 << 11
_FS_MAKE_SYM = 1 << 12
_FS_REFER = 1 << 13  # ABI v2 (kernel 5.19+)
_FS_TRUNCATE = 1 << 14  # ABI v2

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
