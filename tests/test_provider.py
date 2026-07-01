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


if __name__ == "__main__":
    test_verdict_allow_maps_to_allow()
    test_verdict_deny_carries_reason()
    test_verdict_deny_default_reason_is_empty_string()
    print("provider hook-output self-checks: OK")
