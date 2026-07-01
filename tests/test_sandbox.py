from app.core.sandbox import NoopSandbox, SandboxStatus, write_mask_for_abi


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
