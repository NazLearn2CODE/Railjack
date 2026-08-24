import datetime
import subprocess
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from app.main import app
from app import calendar_tasks

client = TestClient(app)

_GIT_CAL_MD = """---
title: Git Sync Calendar
updated: 2026-08-18
category: project
---

# Recurring Schedules
[]

# Dated Tasks
- id: task-1
  date: 2026-08-18
  type: reminder
  title: Check deployment
  status: pending
  tags: [ops]
"""


def _run_git(cwd, *args):
    argv = ["git"]
    if cwd is not None:
        argv += ["-C", str(cwd)]
    argv += list(args)
    proc = subprocess.run(argv, capture_output=True, text=True)
    assert proc.returncode == 0, f"git {args} failed: {proc.stdout}{proc.stderr}"
    return proc


@pytest.fixture
def git_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real git 'vault' (clone + bare remote) with the API pointed at its data file."""
    remote = tmp_path / "CephalonVoid.git"
    _run_git(None, "init", "--bare", str(remote))
    clone = tmp_path / "office" / "Cephalon"
    clone.mkdir(parents=True)
    _run_git(None, "clone", str(remote), str(clone))
    _run_git(clone, "config", "user.email", "tawhan@test")
    _run_git(clone, "config", "user.name", "Tawhan Test")
    data_dir = clone / "20-projects"
    data_dir.mkdir()
    data_file = data_dir / "working-calendar-data.md"
    data_file.write_text(_GIT_CAL_MD, encoding="utf-8")
    _run_git(clone, "add", "-A")
    _run_git(clone, "commit", "-m", "init calendar data")
    _run_git(clone, "push", "-u", "origin", "HEAD")
    monkeypatch.setattr(calendar_tasks, "get_data_path", lambda: data_file)
    return {"remote": remote, "clone": clone, "file": data_file}


@pytest.fixture
def temp_calendar_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    test_file = tmp_path / "20-projects" / "working-calendar-data.md"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("""---
title: Test Calendar Data
updated: 2026-08-18
category: project
---

# Recurring Schedules
- id: monthly-report
  type: prompt_task
  title: Monthly Report Gen
  cron: "0 0 18 * *"
  tags: [report]
  target_repo: /home/user/project
  prompt: |
    ### GOAL: Generate report for {{MONTH_NAME}} {{YEAR}}
    ### GROUND TRUTH: {{TARGET_REPO}}/app/
    ### CONSTRAINTS: Ponytail rules apply.
    ### INSTRUCTIONS:
    1. Run generator for date {{DATE}}.

- id: daily-sync
  type: reminder
  title: Daily Team Sync
  cron: "daily"

# Dated Tasks
- id: task-1
  date: 2026-08-18
  type: reminder
  title: Check deployment
  status: pending
  tags: [ops]

- id: task-2
  date: 2026-08-18
  type: prompt_task
  title: Build module slice
  status: completed
  target_repo: /home/user/app
  prompt: "Do task for {{TODAY}}"
""", encoding="utf-8")

    monkeypatch.setattr(calendar_tasks, "get_data_path", lambda: test_file)
    return test_file


def test_token_interpolation():
    d = datetime.date(2026, 8, 18)
    prompt = "Date: {{DATE}}, Yesterday: {{YESTERDAY}}, Month: {{MONTH_NAME}} {{YEAR}}, Repo: {{TARGET_REPO}}"
    res = calendar_tasks.interpolate_prompt(prompt, d, "/path/to/repo")
    assert res == "Date: 2026-08-18, Yesterday: 2026-08-17, Month: August 2026, Repo: /path/to/repo"


def test_matches_schedule():
    # 2026-08-18 is a Tuesday (isoweekday 2)
    d = datetime.date(2026, 8, 18)
    assert calendar_tasks.matches_schedule("daily", d) is True
    assert calendar_tasks.matches_schedule("monthly:18", d) is True
    assert calendar_tasks.matches_schedule("monthly:19", d) is False
    assert calendar_tasks.matches_schedule("0 0 18 * *", d) is True
    assert calendar_tasks.matches_schedule("0 0 19 * *", d) is False
    # Tuesday is dow 2
    assert calendar_tasks.matches_schedule("0 0 * * 2", d) is True
    assert calendar_tasks.matches_schedule("0 0 * * 1", d) is False


def test_get_month_overview(temp_calendar_file):
    res = client.get("/api/calendar/month?year=2026&month=8")
    assert res.status_code == 200
    data = res.json()
    assert data["year"] == 2026
    assert data["month"] == 8
    assert data["month_name"] == "August"
    assert "2026-08-18" in data["days"]
    day_18 = data["days"]["2026-08-18"]
    # 2 dated (1 reminder, 1 prompt) + 2 recurring (1 monthly report, 1 daily sync)
    assert day_18["total"] == 4
    assert day_18["completed"] == 1


def test_get_day_tasks(temp_calendar_file):
    res = client.get("/api/calendar/day?date=2026-08-18")
    assert res.status_code == 200
    data = res.json()
    assert data["date"] == "2026-08-18"
    tasks = data["tasks"]
    assert len(tasks) == 4
    prompt_task = next(t for t in tasks if t["id"] == "task-2")
    assert prompt_task["interpolated_prompt"] == "Do task for 2026-08-18"

    rec_task = next(t for t in tasks if "monthly-report" in t["id"])
    assert "Generate report for August 2026" in rec_task["interpolated_prompt"]


def test_create_and_delete_task(temp_calendar_file):
    # Create dated task
    create_res = client.post("/api/calendar/task", json={
        "date": "2026-08-20",
        "type": "reminder",
        "title": "New Reminder Test",
        "tags": ["test"]
    })
    assert create_res.status_code == 200
    created_id = create_res.json()["task"]["id"]

    # Verify on day
    day_res = client.get("/api/calendar/day?date=2026-08-20")
    assert any(t["id"] == created_id for t in day_res.json()["tasks"])

    # Update status
    patch_res = client.patch(f"/api/calendar/task/{created_id}/status", json={"status": "completed"})
    assert patch_res.status_code == 200
    assert patch_res.json()["new_status"] == "completed"

    # Delete task
    del_res = client.delete(f"/api/calendar/task/{created_id}")
    assert del_res.status_code == 200

    # Verify deleted
    day_after = client.get("/api/calendar/day?date=2026-08-20")
    assert not any(t["id"] == created_id for t in day_after.json()["tasks"])


def test_update_recurring_task_status_with_hyphens(temp_calendar_file):
    # Update status of materialized recurring task id 'rec-monthly-report-2026-08-18'
    rec_id = "rec-monthly-report-2026-08-18"
    patch_res = client.patch(f"/api/calendar/task/{rec_id}/status", json={"status": "completed"})
    assert patch_res.status_code == 200
    assert patch_res.json()["new_status"] == "completed"

    # Verify on day
    day_res = client.get("/api/calendar/day?date=2026-08-18")
    assert day_res.status_code == 200
    rec_task = next(t for t in day_res.json()["tasks"] if t["id"] == rec_id)
    assert rec_task["status"] == "completed"


def test_dispatch_prompt(temp_calendar_file, monkeypatch):
    dispatched = []

    def fake_insert(text):
        dispatched.append(text)
        return {"status": "ok"}

    monkeypatch.setattr(calendar_tasks, "insert_text", fake_insert)

    res = client.post("/api/calendar/task/task-2/dispatch")
    assert res.status_code == 200
    assert len(dispatched) == 1
    assert "Do task for 2026-08-18" in dispatched[0]


def test_dispatch_multiline_prompt_collapsed(temp_calendar_file, monkeypatch):
    """Railjack divergence from Somatic: multi-line prompts are collapsed to one
    line before insert_text, which (like the HTTP path) rejects control chars."""
    dispatched = []

    def fake_insert(text):
        dispatched.append(text)
        return {"status": "ok"}

    monkeypatch.setattr(calendar_tasks, "insert_text", fake_insert)

    res = client.post(
        "/api/calendar/task/rec-monthly-report-2026-08-18/dispatch",
        json={"date": "2026-08-18"},
    )
    assert res.status_code == 200
    assert len(dispatched) == 1
    assert "\n" not in dispatched[0]
    assert "Generate report for August 2026" in dispatched[0]


def test_write_syncs_to_remote(git_vault):
    before = _run_git(git_vault["remote"], "rev-parse", "HEAD").stdout.strip()
    res = client.patch("/api/calendar/task/task-1/status", json={"status": "completed"})
    assert res.status_code == 200
    sync = res.json()["sync"]
    assert sync["status"] == "ok"
    assert sync["committed"] is True
    assert sync["pushed"] is True
    after = _run_git(git_vault["remote"], "rev-parse", "HEAD").stdout.strip()
    assert after != before
    subject = _run_git(git_vault["remote"], "log", "-1", "--format=%s").stdout.strip()
    assert subject == "data(calendar): auto-sync"


def test_sync_pulls_remote_changes(git_vault, tmp_path):
    # Simulate the other machine: clone, add a task, push.
    other = tmp_path / "somatic" / "Cephalon"
    other.mkdir(parents=True)
    _run_git(None, "clone", str(git_vault["remote"]), str(other))
    _run_git(other, "config", "user.email", "tasai@test")
    _run_git(other, "config", "user.name", "Tasai Test")
    other_file = other / "20-projects" / "working-calendar-data.md"
    other_file.write_text(
        _GIT_CAL_MD
        + "- id: task-9\n"
        "  date: 2026-08-21\n"
        "  type: reminder\n"
        "  title: Remote-added task\n"
        "  status: pending\n",
        encoding="utf-8",
    )
    _run_git(other, "add", "-A")
    _run_git(other, "commit", "-m", "office-side add")
    _run_git(other, "push")

    res = client.post("/api/calendar/sync")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

    day = client.get("/api/calendar/day?date=2026-08-21")
    assert day.status_code == 200
    ids = [t["id"] for t in day.json()["tasks"]]
    assert "task-9" in ids


def test_sync_conflict_aborts_rebase_and_keeps_local(git_vault, tmp_path):
    # Other machine flips task-1 to in_progress and pushes.
    other = tmp_path / "somatic" / "Cephalon"
    other.mkdir(parents=True)
    _run_git(None, "clone", str(git_vault["remote"]), str(other))
    _run_git(other, "config", "user.email", "tasai@test")
    _run_git(other, "config", "user.name", "Tasai Test")
    other_file = other / "20-projects" / "working-calendar-data.md"
    other_file.write_text(_GIT_CAL_MD.replace("status: pending", "status: in_progress"), encoding="utf-8")
    _run_git(other, "add", "-A")
    _run_git(other, "commit", "-m", "office-side flip")
    _run_git(other, "push")

    # Local divergent edit to the same region: PATCH commits, pull rebases,
    # conflicts, aborts — and must leave the repo clean, change kept locally.
    res = client.patch("/api/calendar/task/task-1/status", json={"status": "completed"})
    assert res.status_code == 200
    assert res.json()["sync"]["status"] == "conflict"

    assert not (git_vault["clone"] / ".git" / "rebase-merge").exists()
    assert not (git_vault["clone"] / ".git" / "rebase-apply").exists()
    _fm, _rec, dated = calendar_tasks.parse_calendar_file()
    assert any(t["id"] == "task-1" and t["status"] == "completed" for t in dated)


def test_sync_without_repo_soft_fails(temp_calendar_file):
    res = client.post("/api/calendar/sync")
    assert res.status_code == 200
    assert res.json()["status"] == "no-repo"
    # reads still work
    day = client.get("/api/calendar/day?date=2026-08-18")
    assert day.status_code == 200


def test_delete_recurring_occurrence_only(temp_calendar_file):
    """DELETE with a date skips just that occurrence; other occurrences survive."""
    res = client.delete(
        "/api/calendar/task/rec-daily-sync-2026-08-18?date=2026-08-18"
    )
    assert res.status_code == 200
    assert res.json()["skip_date"] == "2026-08-18"

    # That date no longer materializes the schedule…
    day = client.get("/api/calendar/day?date=2026-08-18").json()
    assert not any("daily-sync" in t["id"] for t in day["tasks"])
    month = client.get("/api/calendar/month?year=2026&month=8").json()
    assert month["days"]["2026-08-18"]["total"] == 3  # was 4

    # …but the next day still does, and the schedule persists.
    day_next = client.get("/api/calendar/day?date=2026-08-19").json()
    assert any("daily-sync" in t["id"] for t in day_next["tasks"])
    _fm, recurring, _dated = calendar_tasks.parse_calendar_file()
    rec = next(r for r in recurring if r["id"] == "daily-sync")
    assert rec["skip_dates"] == ["2026-08-18"]


def test_delete_recurring_occurrence_then_all(temp_calendar_file):
    """Occurrence delete then delete-all removes the schedule entirely."""
    res = client.delete(
        "/api/calendar/task/rec-daily-sync-2026-08-18?date=2026-08-18"
    )
    assert res.status_code == 200

    all_res = client.delete("/api/calendar/task/daily-sync")
    assert all_res.status_code == 200

    _fm, recurring, _dated = calendar_tasks.parse_calendar_file()
    assert not any(r["id"] == "daily-sync" for r in recurring)
    day = client.get("/api/calendar/day?date=2026-08-19").json()
    assert not any("daily-sync" in t["id"] for t in day["tasks"])


def test_delete_recurring_materialized_instance_scope(temp_calendar_file):
    """Deleting a completed recurring instance (stored as dated rec-*) with date
    removes the dated override AND skips the occurrence."""
    client.patch(
        "/api/calendar/task/rec-daily-sync-2026-08-18/status",
        json={"status": "completed"},
    )
    res = client.delete("/api/calendar/task/rec-daily-sync-2026-08-18")
    assert res.status_code == 200
    # No date param on a rec- id: the embedded date scopes it.
    assert res.json()["skip_date"] == "2026-08-18"
    day = client.get("/api/calendar/day?date=2026-08-18").json()
    assert not any("daily-sync" in t["id"] for t in day["tasks"])
    day_next = client.get("/api/calendar/day?date=2026-08-19").json()
    assert any("daily-sync" in t["id"] for t in day_next["tasks"])


def test_note_roundtrip(temp_calendar_file):
    """Optional free-text note persists through the markdown schema."""
    create = client.post("/api/calendar/task", json={
        "date": "2026-08-22",
        "type": "reminder",
        "title": "Note test",
        "note": "See https://example.com/wire and www.example.org",
    })
    assert create.status_code == 200
    task_id = create.json()["task"]["id"]

    day = client.get("/api/calendar/day?date=2026-08-22").json()
    task = next(t for t in day["tasks"] if t["id"] == task_id)
    assert task["note"] == "See https://example.com/wire and www.example.org"

    _fm, recurring, dated = calendar_tasks.parse_calendar_file()
    stored = next(t for t in dated if t["id"] == task_id)
    assert stored["note"].startswith("See https://")
