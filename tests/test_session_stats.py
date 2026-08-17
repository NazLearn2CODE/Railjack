"""Session telemetry test suite — limit table, empirical clamp, provider detection, quota parsers."""

from app import session_stats


def test_limit_table_glm_1m():
    assert session_stats._limit_for("glm-5.2", 1000) == 1_000_000
    assert session_stats._limit_for("glm-5.3", 1000) == 1_000_000


def test_limit_table_claude_sonnet_1m():
    session_stats._max_seen.pop("claude-sonnet-5", None)
    assert session_stats._limit_for("claude-sonnet-5", 1000) == 1_000_000


def test_limit_table_claude_haiku_200k():
    session_stats._max_seen.pop("claude-haiku", None)
    assert session_stats._limit_for("claude-haiku", 1000) == 200_000


def test_limit_table_fable_1m():
    session_stats._max_seen.pop("claude-fable-5", None)
    assert session_stats._limit_for("claude-fable-5", 1000) == 1_000_000


def test_empirical_clamp_lifts_stale_limit():
    session_stats._max_seen.pop("test-only-model", None)
    assert session_stats._limit_for("test-only-model", 250_000) == 250_000
    # and it never drops back below the observed max
    assert session_stats._limit_for("test-only-model", 100) == 250_000


def test_provider_detection():
    assert session_stats._provider_for("claude-opus-4-8") == "claude"
    assert session_stats._provider_for("glm-5.2") == "zai"
    assert session_stats._provider_for("cco-glm-5.2") == "cco"
    assert session_stats._provider_for("gemini-3.6-flash-high") == "gemini"
    assert session_stats._provider_for("mystery-9") == "unknown"


def test_parse_zai_usage_maps_both_quotas():
    # SES + RESET both come from TOKENS_LIMIT (the binding coding-plan quota).
    # TIME_LIMIT is the 5-min burst limiter — ignored entirely. WK is dropped.
    resp = {"code": 0, "success": True, "data": {"level": "lite", "limits": [
        {"type": "TIME_LIMIT", "percentage": 42.0,
         "nextResetTime": 9999999999999, "usage": 210},
        {"type": "TOKENS_LIMIT", "percentage": 13.0,
         "nextResetTime": 1700000000000},
    ]}}
    assert session_stats._parse_zai_usage(resp) == {
        "session_pct": 13, "reset_at": "2023-11-14T22:13:20Z"}


def test_parse_zai_usage_missing_returns_none():
    assert session_stats._parse_zai_usage({"data": {}}) is None
    assert session_stats._parse_zai_usage({}) is None


def test_parse_ag_groups_both_groups_with_weekly_reset():
    # Shape from the live local LanguageServer RPC (2026-08-17). remaining-% is
    # inverted to used-%; weekly buckets carry week_pct + week_reset_at.
    resp = {"groups": [
        {"displayName": "Gemini Models", "buckets": [
            {"bucketId": "gemini-weekly", "window": "weekly",
             "remainingFraction": 0.96066433, "resetTime": "2026-08-22T02:50:22Z"},
            {"bucketId": "gemini-5h", "window": "5h",
             "remainingFraction": 0.9207177, "resetTime": "2026-08-17T06:26:00Z"}]},
        {"displayName": "Claude and GPT models", "buckets": [
            {"bucketId": "3p-weekly", "window": "weekly",
             "remainingFraction": 1, "resetTime": "2026-08-23T12:30:44Z"},
            {"bucketId": "3p-5h", "window": "5h",
             "remainingFraction": 1, "resetTime": "2026-08-17T10:10:31Z"}]}]}
    out = session_stats._parse_ag_groups(resp)
    assert out["gemini"] == {
        "session_pct": 8, "reset_at": "2026-08-17T06:26:00Z",
        "week_pct": 4, "week_reset_at": "2026-08-22T02:50:22Z"}
    assert out["3p"] == {
        "session_pct": 0, "reset_at": "2026-08-17T10:10:31Z",
        "week_pct": 0, "week_reset_at": "2026-08-23T12:30:44Z"}


def test_norm_ag_model_strips_effort_suffix():
    assert session_stats._norm_ag_model("Gemini 3.7 Flash (Medium)") == "gemini-3.7-flash"
    assert session_stats._norm_ag_model("") == ""


def test_ag_disk_cache_split_ttl(tmp_path, monkeypatch):
    # Weekly usage can only change while an Antigravity session runs, so the
    # week fields outlive the run (6h); the 5h window rolls on wall time, so
    # its fields expire with the usual 10-min last-good TTL.
    cache = tmp_path / "ag-quota.json"
    monkeypatch.setattr(session_stats, "_AG_CACHE_FILE", cache)
    full = {"gemini": {"session_pct": 8, "reset_at": "2026-08-17T06:26:00Z",
                       "week_pct": 4, "week_reset_at": "2026-08-22T02:50:22Z"}}
    session_stats._save_ag_disk(full)
    assert session_stats._load_ag_disk() == full  # fresh: everything kept

    stale_session = {"ts": session_stats.time.time() - 700, "groups": full}
    cache.write_text(session_stats.json.dumps(stale_session))
    assert session_stats._load_ag_disk() == {
        "gemini": {"week_pct": 4, "week_reset_at": "2026-08-22T02:50:22Z"}}

    stale_all = {"ts": session_stats.time.time() - 7 * 3600, "groups": full}
    cache.write_text(session_stats.json.dumps(stale_all))
    assert session_stats._load_ag_disk() == {}

    cache.write_text("not json")
    assert session_stats._load_ag_disk() == {}  # torn write → empty, never crash
