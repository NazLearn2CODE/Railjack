"""Blueprint §2.2 Layer 3 — Landlock write-confinement (self-sandbox at startup).

Restricts the Orbiter process (and its inherited subprocess tree: the claude-agent-sdk
CLI + its native Bash) to a small set of writable roots. WRITE-type accesses are
confined; reads/execute everywhere stay open.

Fail-open: if Landlock is unavailable (EPERM/ENOSYS/EOPNOTSUPP, non-Linux, locked-down
kernel, or a sandboxed dev shell), apply() returns inactive and Orbiter continues on the
existing L1/L2/approval floor. See ADR 2026-07-01-sandbox-l3-landlock.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import logging
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
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


# --------------------------------------------------------------- Landlock glue
# x86_64 syscall numbers (this box's glibc doesn't expose the landlock_* symbols).
_NR_CREATE_RULESET = 444
_NR_ADD_RULE = 445
_NR_RESTRICT_SELF = 446
_RULE_PATH_BENEATH = 1


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
        ret = libc.syscall(
            ctypes.c_long(_NR_CREATE_RULESET), None, ctypes.c_size_t(0), ctypes.c_uint32(0)
        )
        if ret < 0:
            raise _LandlockUnavailable(ctypes.get_errno())
        return int(ret)

    def apply(self) -> SandboxStatus:
        # 1. Probe + mask. Fail-open on any unavailability.
        abi: int
        try:
            libc = _load_libc()
            abi = self._probe_abi(libc)
        except _LandlockUnavailable as e:
            logger.warning(
                "Landlock unavailable (errno %s=%s) — fail-open on L1/L2/approval",
                e.errno,
                errno.errorcode.get(e.errno, "?"),
            )
            return SandboxStatus(
                active=False, mechanism="landlock", reason=f"unavailable: errno {e.errno}"
            )
        handled = write_mask_for_abi(abi)

        # 2. Build ruleset, add a rule per root, restrict self.
        ruleset_fd = -1
        try:
            attr = _RulesetAttr(handled_access_fs=handled)
            ruleset_fd = libc.syscall(
                ctypes.c_long(_NR_CREATE_RULESET),
                ctypes.byref(attr),
                ctypes.c_size_t(ctypes.sizeof(attr)),
                ctypes.c_uint32(0),
            )
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
                    rc = libc.syscall(
                        ctypes.c_long(_NR_ADD_RULE),
                        ctypes.c_int(ruleset_fd),
                        ctypes.c_int(_RULE_PATH_BENEATH),
                        ctypes.byref(pb),
                        ctypes.c_uint32(0),
                    )
                    if rc < 0:
                        logger.warning(
                            "Landlock add_rule failed for %s (errno %s)",
                            root,
                            errno.errorcode.get(ctypes.get_errno(), "?"),
                        )
                finally:
                    os.close(parent_fd)

            rc = libc.syscall(
                ctypes.c_long(_NR_RESTRICT_SELF), ctypes.c_int(ruleset_fd), ctypes.c_uint32(0)
            )
            if rc < 0:
                raise _LandlockUnavailable(ctypes.get_errno())
        except _LandlockUnavailable as e:
            logger.warning("Landlock restrict failed (errno %s) — fail-open", e.errno)
            return SandboxStatus(
                active=False, mechanism="landlock", abi=abi, reason=f"restrict failed: errno {e.errno}"
            )
        finally:
            if ruleset_fd >= 0:
                with suppress(OSError):
                    os.close(ruleset_fd)  # restrictions persist after close

        logger.info("Landlock sandbox ACTIVE (ABI %s). Writable roots: %s", abi, self._roots)
        return SandboxStatus(
            active=True, mechanism="landlock", abi=abi, writable_roots=list(self._roots)
        )
