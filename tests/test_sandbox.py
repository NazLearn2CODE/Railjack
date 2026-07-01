from app.core.sandbox import NoopSandbox, SandboxStatus


def test_noop_sandbox_returns_inactive_status():
    status = NoopSandbox().apply()
    assert isinstance(status, SandboxStatus)
    assert status.active is False
    assert status.mechanism == "none"
    assert status.reason  # non-empty
    assert status.abi is None
    assert status.writable_roots == []
