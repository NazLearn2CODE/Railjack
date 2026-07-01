"""Self-check for the ClaudeSdkProvider hook-output translation.

The OS-policy verdict (ToolDecision) is produced in app.core.agent; this checks
the provider adapter that turns it into the SDK's PreToolUse hookSpecificOutput.
Run: .venv/bin/python -m pytest tests/test_provider.py -q
"""
from app.core.provider import ClaudeSdkProvider, ToolDecision


def test_verdict_allow_maps_to_allow():
    out = ClaudeSdkProvider._verdict_to_hook_output(ToolDecision(allow=True))
    h = out["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse"
    assert h["permissionDecision"] == "allow"
    assert "permissionDecisionReason" not in h


def test_verdict_deny_carries_reason():
    out = ClaudeSdkProvider._verdict_to_hook_output(ToolDecision(allow=False, reason="nope"))
    h = out["hookSpecificOutput"]
    assert h["permissionDecision"] == "deny"
    assert h["permissionDecisionReason"] == "nope"


def test_verdict_deny_default_reason_is_empty_string():
    # A deny without an explicit reason still serializes cleanly.
    out = ClaudeSdkProvider._verdict_to_hook_output(ToolDecision(allow=False))
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == ""


# --- External MCP server merge (blueprint §3.2) ---
def test_merge_returns_none_when_nothing_configured():
    assert ClaudeSdkProvider._merge_mcp_servers(None, None) is None
    assert ClaudeSdkProvider._merge_mcp_servers({}, None) is None


def test_merge_preserves_external_specs():
    ext = {"fs": {"type": "stdio", "command": "npx", "args": ["-y", "fs-mcp"]}}
    merged = ClaudeSdkProvider._merge_mcp_servers(ext, None)
    assert merged == ext


def test_merge_adds_orbiter_delegate_server_for_supervisor():
    ext = {"gh": {"type": "http", "url": "https://example/mcp"}}
    merged = ClaudeSdkProvider._merge_mcp_servers(ext, "DELEGATE_SERVER")
    assert merged["gh"]["url"] == "https://example/mcp"
    assert merged["orbiter"] == "DELEGATE_SERVER"  # supervisor's delegate tool coexists


def test_merge_orbiter_key_wins_over_external_collision():
    # A supervisor always gets its delegate under the reserved "orbiter" key.
    ext = {"orbiter": {"type": "stdio", "command": "imposter"}}
    merged = ClaudeSdkProvider._merge_mcp_servers(ext, "DELEGATE_SERVER")
    assert merged["orbiter"] == "DELEGATE_SERVER"


if __name__ == "__main__":
    test_verdict_allow_maps_to_allow()
    test_verdict_deny_carries_reason()
    test_verdict_deny_default_reason_is_empty_string()
    test_merge_returns_none_when_nothing_configured()
    test_merge_preserves_external_specs()
    test_merge_adds_orbiter_delegate_server_for_supervisor()
    test_merge_orbiter_key_wins_over_external_collision()
    print("provider self-checks: OK")
