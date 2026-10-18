"""Session telemetry test suite — limit table, empirical clamp, provider detection, quota parsers."""

from app.platform import session_stats


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


def test_ag_disk_cache_validity_bounds(tmp_path, monkeypatch):
    # Both windows are fixed and usage only changes while an Antigravity
    # session runs, so a cached reading stays true while idle: session (5h)
    # fields until their own reset_at passes (age is irrelevant), week fields
    # for the 6h sanity cap.
    import datetime as _dt
    cache = tmp_path / "ag-quota.json"
    monkeypatch.setattr(session_stats, "_AG_CACHE_FILE", cache)
    future = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=2)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    past = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
    full = {"gemini": {"session_pct": 8, "reset_at": future,
                       "week_pct": 4, "week_reset_at": "2026-08-22T02:50:22Z"}}

    session_stats._save_ag_disk(full)
    assert session_stats._load_ag_disk() == full  # window live: everything kept

    # old sample, still-live window → session fields persist (no 10-min TTL)
    old_but_live = {"ts": session_stats.time.time() - 3600, "groups": full}
    cache.write_text(session_stats.json.dumps(old_but_live))
    assert session_stats._load_ag_disk() == full

    # window expired → session fields drop, week fields stay under the 6h cap
    expired = {"gemini": {"session_pct": 8, "reset_at": past,
                          "week_pct": 4, "week_reset_at": "2026-08-22T02:50:22Z"}}
    cache.write_text(session_stats.json.dumps(
        {"ts": session_stats.time.time() - 700, "groups": expired}))
    assert session_stats._load_ag_disk() == {
        "gemini": {"week_pct": 4, "week_reset_at": "2026-08-22T02:50:22Z"}}

    # everything out of validity → {}
    cache.write_text(session_stats.json.dumps(
        {"ts": session_stats.time.time() - 7 * 3600, "groups": expired}))
    assert session_stats._load_ag_disk() == {}

    cache.write_text("not json")
    assert session_stats._load_ag_disk() == {}  # torn write → empty, never crash


def test_agy_cli_quota_parses_slash_output(monkeypatch):
    """agy 1.3.3 CSRF-locked the RPC; `agy -p /quota` is the scrape path.
    CLI prints REMAINING % — the parser must invert to used %."""
    sample = (
        "Gemini Models\tWeekly Limit Remaining\t39%\t2026-10-14T03:07:27Z\n"
        "Gemini Models\tFive Hour Limit Remaining\t70%\t2026-10-11T14:32:03Z\n"
        "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-10-18T10:36:15Z\n"
        "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-10-11T15:36:15Z\n"
    )

    class R:
        stdout = sample

    monkeypatch.setattr(session_stats.subprocess, "run", lambda *a, **k: R())
    out = session_stats._agy_cli_quota()
    assert out["gemini"]["week_pct"] == 61
    assert out["gemini"]["week_reset_at"] == "2026-10-14T03:07:27Z"
    assert out["gemini"]["session_pct"] == 30
    assert out["gemini"]["reset_at"] == "2026-10-11T14:32:03Z"
    assert out["3p"]["week_pct"] == 0
    assert out["3p"]["session_pct"] == 0


def test_agy_cli_quota_garbage_returns_empty(monkeypatch):
    class R:
        stdout = "some unparseable noise\n"

    monkeypatch.setattr(session_stats.subprocess, "run", lambda *a, **k: R())
    assert session_stats._agy_cli_quota() == {}


def test_agy_cli_quota_argon_split_and_local_resets(monkeypatch):
    """Real agy output (2026-10-18): space-aligned columns, '+07' resets, and a
    'Gemini 4 Argon' row that must NOT overwrite the Gemini Models weekly."""
    sample = (
        "Quota:\n"
        "Gemini Models          Weekly Limit Remaining       99%   2026-10-24 20:26 +07\n"
        "Gemini Models          Five Hour Limit Remaining    98%   2026-10-18 18:33 +07\n"
        "Claude and GPT models  Weekly Limit Remaining       100%  2026-10-25 15:46 +07\n"
        "Claude and GPT models  Five Hour Limit Remaining    100%  2026-10-18 20:46 +07\n"
        "Gemini 4 Argon         Gemini 4 Argon Weekly Limit  52%   2026-10-24 20:25 +07\n"
    )

    class R:
        stdout = sample

    monkeypatch.setattr(session_stats.subprocess, "run", lambda *a, **k: R())
    out = session_stats._agy_cli_quota()
    assert out["gemini"] == {"week_pct": 1, "week_reset_at": "2026-10-24T13:26:00Z",
                             "session_pct": 2, "reset_at": "2026-10-18T11:33:00Z"}
    assert out["argon"] == {"week_pct": 48, "week_reset_at": "2026-10-24T13:25:00Z"}
    assert out["3p"]["reset_at"] == "2026-10-18T13:46:00Z"

    view = session_stats._agy_groups_view(out)
    assert view["argon"] == {"weekly": {"remaining_pct": 52, "reset_at": "2026-10-24T13:25:00Z"}}
    assert view["gemini"]["five_hour"] == {"remaining_pct": 98, "reset_at": "2026-10-18T11:33:00Z"}
    assert view["gemini"]["weekly"]["remaining_pct"] == 99
    assert view["3p"]["weekly"]["remaining_pct"] == 100


def test_cli_reset_iso_passthrough():
    assert session_stats._cli_reset_iso("2026-10-14T03:07:27Z") == "2026-10-14T03:07:27Z"
    assert session_stats._cli_reset_iso("2026-10-18 18:33:05 +0530") == "2026-10-18T13:03:05Z"
    assert session_stats._cli_reset_iso("soon") == "soon"
