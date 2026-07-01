import os
import tempfile
from pathlib import Path

import pytest

from app.core.sandbox import (
    LandlockSandbox,
    NoopSandbox,
    SandboxStatus,
    normalize_roots,
    write_mask_for_abi,
)


def test_noop_sandbox_returns_inactive_status():
    status = NoopSandbox().apply()
    assert isinstance(status, SandboxStatus)
    assert status.active is False
    assert status.mechanism == "none"
    assert status.reason  # non-empty
    assert status.abi is None
    assert status.writable_roots == []


def test_write_mask_abi_v1_excludes_refer_truncate():
    # ABI 1: write bits within 0x1FFF; REFER (13) / TRUNCATE (14) NOT set.
    mask = write_mask_for_abi(1)
    assert mask & 0x1FFF == mask  # no bits above the v1 range
    assert mask & (1 << 13) == 0  # REFER absent
    assert mask & (1 << 14) == 0  # TRUNCATE absent
    assert mask & (1 << 1) != 0  # WRITE_FILE present


def test_write_mask_abi_v2_includes_refer_truncate():
    # ABI >=2: adds REFER + TRUNCATE.
    mask = write_mask_for_abi(2)
    assert mask & (1 << 13) != 0  # REFER present
    assert mask & (1 << 14) != 0  # TRUNCATE present
    assert mask & 0x7FFF == mask  # within v2 range


def test_normalize_roots_expands_and_dedups_preserving_order():
    roots = [Path("~/foo"), Path("/tmp"), Path("~/foo/bar")]
    # extra duplicates /tmp and adds one new
    out = normalize_roots(roots, extra="/tmp:/opt/orbiter")
    assert out[0].endswith("/foo")
    assert out[1] == "/tmp"
    # endswith, not ==: resolve() follows symlinks, and on ostree hosts /opt → /var/opt.
    assert out[-1].endswith("/orbiter")
    assert out.count("/tmp") == 1  # dedup
    assert all(p.startswith("/") for p in out)  # absolutized


# --- Gated live self-check (the only validation of the actual syscalls) ---
# Locks down the process that runs it, so it is OFF by default. Run against a
# REAL Orbiter / bare Python process, not the Claude Code shell (Landlock probes
# EPERM there — the fail-open path is correct, L3 is simply inactive).
_LIVE = os.environ.get("ORBITER_SANDBOX_LIVE") == "1"


@pytest.mark.skipif(not _LIVE, reason="gated: locks down the process; run vs a real Orbiter process")
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
