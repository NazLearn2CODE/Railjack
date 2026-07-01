from app.core.sandbox import NoopSandbox, SandboxStatus, normalize_roots, write_mask_for_abi


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
    from pathlib import Path

    roots = [Path("~/foo"), Path("/tmp"), Path("~/foo/bar")]
    # extra duplicates /tmp and adds one new
    out = normalize_roots(roots, extra="/tmp:/opt/orbiter")
    assert out[0].endswith("/foo")
    assert out[1] == "/tmp"
    # endswith, not ==: resolve() follows symlinks, and on ostree hosts /opt → /var/opt.
    assert out[-1].endswith("/orbiter")
    assert out.count("/tmp") == 1  # dedup
    assert all(p.startswith("/") for p in out)  # absolutized
