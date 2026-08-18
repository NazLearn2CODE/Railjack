import datetime
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from app.main import app
from app import calendar_tasks

client = TestClient(app)


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
