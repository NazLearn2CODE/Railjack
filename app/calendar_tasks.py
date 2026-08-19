"""Working Calendar Tasks & Prompt Launcher backend module.

Parses, stores, and evaluates date-anchored reminders, recurring schedules, and
prompt-ready task cards from the Cephalon vault (`20-projects/working-calendar-data.md`).
Provides token interpolation and 1-click terminal insertion.
"""

from __future__ import annotations

import calendar
import datetime
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .terminal_input import insert_text

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

_DEFAULT_VAULT = Path.home() / "Cephalon"
_LOCAL_FALLBACK = Path.home() / ".config" / "railjack"

logger = logging.getLogger(__name__)

_AUTOSYNC_MSG = "data(calendar): auto-sync"
_SYNC_LOCK = threading.Lock()
_READ_SYNC_INTERVAL = 60.0  # seconds between background syncs on read endpoints
_last_read_sync = 0.0


def get_data_path() -> Path:
    vault_override = os.environ.get("CEPHALON_VAULT_PATH")
    if vault_override:
        p = Path(vault_override) / "20-projects" / "working-calendar-data.md"
        if p.parent.exists():
            return p
    vault_path = _DEFAULT_VAULT / "20-projects" / "working-calendar-data.md"
    if vault_path.parent.exists():
        return vault_path
    _LOCAL_FALLBACK.mkdir(parents=True, exist_ok=True)
    return _LOCAL_FALLBACK / "working-calendar-data.md"


def _find_repo_root(data_path: Path) -> Path | None:
    """Nearest ancestor of data_path that is a git work tree."""
    for cand in data_path.resolve().parents:
        if (cand / ".git").exists():
            return cand
    return None


def _git(repo: Path, *args: str) -> tuple[int, str]:
    """Run a git command in repo; returns (returncode, combined output).

    Hooks are bypassed (core.hooksPath=/dev/null): the vault's post-commit
    hook re-indexes RAG (uv + Ollama embeddings — tens of seconds), which
    would stall every calendar tick and blow the 10s timeout below. Those
    hooks still fire on normal session commits/pulls; the calendar data file
    is machine-parsed app data, not RAG material. This also covers the vault
    pre-commit's vault-check gate and the pull-time telegram-bot restart,
    which a scoped data sync must not trigger.
    """
    try:
        proc = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return 124, "git timed out"
    out = f"{proc.stdout.strip()} {proc.stderr.strip()}".strip()
    return proc.returncode, out


def _read_sync_due() -> bool:
    """True at most once per _READ_SYNC_INTERVAL (throttle for read endpoints)."""
    global _last_read_sync
    now = time.monotonic()
    if now - _last_read_sync < _READ_SYNC_INTERVAL:
        return False
    _last_read_sync = now
    return True


def vault_sync() -> dict[str, Any]:
    """Sync the calendar data file with its vault remote: commit → pull → push.

    Scoped to working-calendar-data.md only — unrelated dirty vault files are
    never staged. Soft-fails everywhere (offline, diverged, no repo): returns
    a status dict instead of raising, so calendar reads/writes never break.
    """
    resolved = get_data_path().resolve()
    repo = _find_repo_root(resolved)
    if repo is None:
        return {"status": "no-repo", "detail": "calendar file is not inside a git repo"}
    rel = resolved.relative_to(repo).as_posix()
    result: dict[str, Any] = {"status": "ok", "committed": False, "pulled": False, "pushed": False, "detail": ""}

    with _SYNC_LOCK:
        rc, out = _git(repo, "add", "--", rel)
        if rc != 0:
            return {**result, "status": "error", "detail": f"git add: {out}"}
        rc, out = _git(repo, "diff", "--cached", "--quiet")
        if rc != 0:
            rc, out = _git(repo, "commit", "-m", _AUTOSYNC_MSG)
            if rc != 0:
                return {**result, "status": "error", "detail": f"git commit: {out}"}
            result["committed"] = True

        rc, out = _git(repo, "pull", "--ff-only")
        if rc != 0:
            # Both sides advanced: replay our data commit on top. On conflict,
            # abort and keep the local commit — rare (one user, two machines);
            # the session-end ritual resolves it.
            rc, out = _git(repo, "pull", "--rebase", "--autostash")
            if rc != 0:
                _git(repo, "rebase", "--abort")
                logger.warning("calendar sync conflict, rebase aborted: %s", out)
                return {**result, "status": "conflict", "detail": f"pull: {out}"}
        result["pulled"] = True

        rc, out = _git(repo, "push")
        if rc != 0:
            logger.warning("calendar sync push failed (local commit kept): %s", out)
            return {**result, "status": "local-only", "detail": f"push: {out}"}
        result["pushed"] = True

    return result


class TaskItem(BaseModel):
    id: str
    date: str | None = None
    type: Literal["reminder", "prompt_task"] = "reminder"
    title: str
    status: Literal["pending", "in_progress", "completed", "snoozed"] = "pending"
    tags: list[str] = Field(default_factory=list)
    target_repo: str | None = None
    prompt: str | None = None
    cron: str | None = None
    is_recurring: bool = False


class TaskCreateBody(BaseModel):
    date: str | None = None
    type: Literal["reminder", "prompt_task"] = "reminder"
    title: str
    status: Literal["pending", "in_progress", "completed", "snoozed"] = "pending"
    tags: list[str] = Field(default_factory=list)
    target_repo: str | None = None
    prompt: str | None = None
    cron: str | None = None


class StatusUpdateBody(BaseModel):
    status: Literal["pending", "in_progress", "completed", "snoozed"]


class DispatchBody(BaseModel):
    date: str | None = None
    custom_prompt: str | None = None


def parse_calendar_file(path: Path | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse working-calendar-data.md into (frontmatter, recurring_schedules, dated_tasks)."""
    p = path or get_data_path()
    if not p.exists():
        return (
            {"title": "Working Calendar Data", "updated": datetime.date.today().isoformat(), "category": "project"},
            [],
            [],
        )

    content = p.read_text(encoding="utf-8")
    frontmatter = {}
    body = content

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                frontmatter = yaml.safe_load(parts[1]) or {}
            except Exception:
                frontmatter = {}
            body = parts[2]

    # Split into sections by heading
    recurring_schedules: list[dict[str, Any]] = []
    dated_tasks: list[dict[str, Any]] = []

    sections = re.split(r"^#\s+(.+)$", body, flags=re.MULTILINE)
    for idx in range(1, len(sections), 2):
        heading = sections[idx].strip().lower()
        sec_body = sections[idx + 1]
        try:
            parsed = yaml.safe_load(sec_body)
            if isinstance(parsed, list):
                # Normalize date fields to ISO strings
                for item in parsed:
                    if isinstance(item, dict) and "date" in item and item["date"] is not None:
                        item["date"] = str(item["date"])
                if "recurring" in heading:
                    recurring_schedules = parsed
                elif "dated" in heading or "task" in heading:
                    dated_tasks = parsed
        except Exception:
            continue

    return frontmatter, recurring_schedules, dated_tasks


def save_calendar_file(
    frontmatter: dict[str, Any],
    recurring_schedules: list[dict[str, Any]],
    dated_tasks: list[dict[str, Any]],
    path: Path | None = None,
) -> None:
    """Atomically writes back working-calendar-data.md."""
    p = path or get_data_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    frontmatter["updated"] = datetime.date.today().isoformat()
    lines = ["---"]
    lines.append(yaml.safe_dump(frontmatter, sort_keys=False).strip())
    lines.append("---\n")

    lines.append("# Recurring Schedules")
    if recurring_schedules:
        lines.append(yaml.safe_dump(recurring_schedules, sort_keys=False).strip())
    else:
        lines.append("[]")
    lines.append("\n# Dated Tasks")
    if dated_tasks:
        lines.append(yaml.safe_dump(dated_tasks, sort_keys=False).strip())
    else:
        lines.append("[]")
    lines.append("")

    new_content = "\n".join(lines)

    dir_name = p.parent
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
        tf.write(new_content)
        temp_name = tf.name

    os.replace(temp_name, p)


def interpolate_prompt(prompt: str | None, target_date: datetime.date | str, target_repo: str | None = None) -> str:
    """Interpolate template tokens in prompt text."""
    if not prompt:
        return ""

    if isinstance(target_date, str):
        try:
            target_date = datetime.date.fromisoformat(target_date)
        except ValueError:
            target_date = datetime.date.today()

    yesterday = target_date - datetime.timedelta(days=1)
    tomorrow = target_date + datetime.timedelta(days=1)
    month_name = target_date.strftime("%B")
    month_str = f"{target_date.month:02d}"
    day_str = f"{target_date.day:02d}"
    year_str = str(target_date.year)
    date_str = target_date.isoformat()
    repo_str = target_repo or ""

    text = prompt
    replacements = {
        "{{DATE}}": date_str,
        "{{TODAY}}": date_str,
        "{{YESTERDAY}}": yesterday.isoformat(),
        "{{TOMORROW}}": tomorrow.isoformat(),
        "{{DAY}}": day_str,
        "{{MONTH}}": month_str,
        "{{MONTH_NAME}}": month_name,
        "{{YEAR}}": year_str,
        "{{TARGET_REPO}}": repo_str,
    }
    for token, val in replacements.items():
        text = text.replace(token, val)
    return text


def matches_schedule(cron_str: str, d: datetime.date) -> bool:
    """Check if a date matches a simple cron schedule string or pattern.

    Supports:
    - 5-part standard cron: 'min hour dom month dow' (e.g. '0 0 18 * *' or '0 0 * * 1')
    - 'daily', 'monthly:DD', 'weekly:D'
    """
    cron_str = cron_str.strip().lower()
    if cron_str in ("daily", "@daily", "0 0 * * *", "* * * * *"):
        return True
    if cron_str.startswith("monthly:"):
        try:
            day_num = int(cron_str.split(":")[1])
            return d.day == day_num
        except Exception:
            return False
    if cron_str.startswith("weekly:"):
        try:
            dow = int(cron_str.split(":")[1])  # 1=Mon, 7=Sun or 0=Sun
            # python isoweekday: 1=Mon, 7=Sun
            return d.isoweekday() == dow or (dow == 0 and d.isoweekday() == 7)
        except Exception:
            return False

    parts = cron_str.split()
    if len(parts) == 5:
        _m, _h, dom, month, dow = parts
        # check month
        if month != "*" and int(month) != d.month:
            return False
        # check dom
        if dom != "*" and int(dom) != d.day:
            return False
        # check dow (0 or 7 = Sunday, 1 = Monday, ..., 6 = Saturday)
        if dow != "*":
            target_dow = int(dow)
            iso_dow = d.isoweekday()  # 1..7
            cron_dow = 0 if iso_dow == 7 else iso_dow
            if target_dow != cron_dow and target_dow != iso_dow:
                return False
        return True

    return False


@router.get("/month")
def get_month_overview(year: int | None = None, month: int | None = None) -> dict[str, Any]:
    """Return task summary counts and status dots for each day in the given month."""
    today = datetime.date.today()
    y = year or today.year
    m = month or today.month

    if _read_sync_due():
        vault_sync()

    _fm, recurring, dated = parse_calendar_file()

    num_days = calendar.monthrange(y, m)[1]
    days_map: dict[str, dict[str, Any]] = {}

    for day in range(1, num_days + 1):
        cur_date = datetime.date(y, m, day)
        date_str = cur_date.isoformat()

        # Find dated tasks for this day
        day_dated = [t for t in dated if t.get("date") == date_str]
        dated_ids = {t.get("id") for t in day_dated}

        # Materialize recurring tasks (excluding ones already stored in dated for this day)
        day_recurring = [
            r for r in recurring
            if r.get("cron")
            and matches_schedule(str(r["cron"]), cur_date)
            and f"rec-{r.get('id')}-{date_str}" not in dated_ids
            and r.get("id") not in dated_ids
        ]

        total_reminders = sum(
            1 for t in day_dated if t.get("type") == "reminder"
        ) + sum(1 for r in day_recurring if r.get("type") == "reminder")

        total_prompts = sum(
            1 for t in day_dated if t.get("type") == "prompt_task"
        ) + sum(1 for r in day_recurring if r.get("type") == "prompt_task")

        completed_count = sum(
            1 for t in day_dated if t.get("status") == "completed"
        )

        total_tasks = len(day_dated) + len(day_recurring)

        if total_tasks > 0:
            days_map[date_str] = {
                "date": date_str,
                "reminders": total_reminders,
                "prompts": total_prompts,
                "completed": completed_count,
                "total": total_tasks,
            }

    return {
        "year": y,
        "month": m,
        "month_name": datetime.date(y, m, 1).strftime("%B"),
        "days": days_map,
    }


@router.get("/day")
def get_day_tasks(date: str | None = None) -> dict[str, Any]:
    """Return detailed tasks for a specific date (tokens interpolated)."""
    target_str = date or datetime.date.today().isoformat()
    try:
        target_date = datetime.date.fromisoformat(target_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format (expected YYYY-MM-DD)")

    if _read_sync_due():
        vault_sync()

    _fm, recurring, dated = parse_calendar_file()

    items: list[dict[str, Any]] = []

    # 1. Dated tasks
    for t in dated:
        if t.get("date") == target_str:
            item = dict(t)
            item["is_recurring"] = False
            if item.get("prompt"):
                item["interpolated_prompt"] = interpolate_prompt(
                    item["prompt"], target_date, item.get("target_repo")
                )
            items.append(item)

    # 2. Materialized recurring tasks (if not overridden by a dated task with same id)
    existing_ids = {t["id"] for t in items if "id" in t}
    for r in recurring:
        rid = f"rec-{r.get('id')}-{target_str}"
        if rid in existing_ids or r.get("id") in existing_ids:
            continue
        if r.get("cron") and matches_schedule(str(r["cron"]), target_date):
            item = dict(r)
            item["id"] = rid
            item["date"] = target_str
            item["is_recurring"] = True
            item["status"] = "pending"
            if item.get("prompt"):
                item["interpolated_prompt"] = interpolate_prompt(
                    item["prompt"], target_date, item.get("target_repo")
                )
            items.append(item)

    return {
        "date": target_str,
        "date_formatted": target_date.strftime("%A, %B %d, %Y"),
        "tasks": items,
    }


@router.post("/task")
def create_task(body: TaskCreateBody) -> dict[str, Any]:
    """Create a new dated task or recurring schedule."""
    fm, recurring, dated = parse_calendar_file()

    new_id = f"task-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"

    if body.cron:
        # Recurring task
        item = {
            "id": new_id,
            "type": body.type,
            "title": body.title,
            "cron": body.cron,
            "tags": body.tags,
        }
        if body.target_repo:
            item["target_repo"] = body.target_repo
        if body.prompt:
            item["prompt"] = body.prompt
        recurring.append(item)
    else:
        # Dated task
        d_str = body.date or datetime.date.today().isoformat()
        item = {
            "id": new_id,
            "date": d_str,
            "type": body.type,
            "title": body.title,
            "status": body.status,
            "tags": body.tags,
        }
        if body.target_repo:
            item["target_repo"] = body.target_repo
        if body.prompt:
            item["prompt"] = body.prompt
        dated.append(item)

    save_calendar_file(fm, recurring, dated)
    return {"status": "ok", "task": item, "sync": vault_sync()}


@router.patch("/task/{task_id}/status")
def update_task_status(task_id: str, body: StatusUpdateBody) -> dict[str, Any]:
    """Update status of a dated task (or materialize & update a recurring task instance)."""
    fm, recurring, dated = parse_calendar_file()

    # Check if existing in dated
    found = False
    for t in dated:
        if t.get("id") == task_id:
            t["status"] = body.status
            found = True
            break

    # If it is a materialized recurring task id (e.g. rec-<orig_id>-<YYYY-MM-DD>)
    if not found and task_id.startswith("rec-"):
        m = re.match(r"^rec-(.+)-(\d{4}-\d{2}-\d{2})$", task_id)
        if m:
            orig_id = m.group(1)
            date_str = m.group(2)
            # Find in recurring
            for r in recurring:
                if r.get("id") == orig_id:
                    new_item = dict(r)
                    new_item["id"] = task_id
                    new_item["date"] = date_str
                    new_item["status"] = body.status
                    dated.append(new_item)
                    found = True
                    break

    if not found:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    save_calendar_file(fm, recurring, dated)
    return {"status": "ok", "id": task_id, "new_status": body.status, "sync": vault_sync()}


@router.delete("/task/{task_id}")
def delete_task(task_id: str) -> dict[str, Any]:
    """Delete a task or recurring schedule."""
    fm, recurring, dated = parse_calendar_file()

    orig_len_dated = len(dated)
    orig_len_rec = len(recurring)

    dated = [t for t in dated if t.get("id") != task_id]
    recurring = [r for r in recurring if r.get("id") != task_id]

    if len(dated) == orig_len_dated and len(recurring) == orig_len_rec:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    save_calendar_file(fm, recurring, dated)
    return {"status": "ok", "deleted_id": task_id, "sync": vault_sync()}


@router.post("/sync")
def sync_calendar() -> dict[str, Any]:
    """Force a full vault sync now (the calendar panel's SYNC button)."""
    return vault_sync()


@router.post("/task/{task_id}/dispatch")
def dispatch_task_prompt(task_id: str, body: DispatchBody | None = None) -> dict[str, Any]:
    """Dispatch the prompt of a task directly into the active terminal via terminal_input."""
    prompt_text = body.custom_prompt if body and body.custom_prompt else None

    if not prompt_text:
        # Retrieve and interpolate prompt from task
        _fm, recurring, dated = parse_calendar_file()
        target_task = next((t for t in dated if t.get("id") == task_id), None)
        if not target_task:
            target_task = next((r for r in recurring if r.get("id") == task_id or f"rec-{r.get('id')}" in task_id), None)

        if not target_task or not target_task.get("prompt"):
            raise HTTPException(status_code=404, detail="Task or prompt not found")

        t_val = (body.date if body and body.date else None) or target_task.get("date") or datetime.date.today().isoformat()
        prompt_text = interpolate_prompt(target_task.get("prompt"), t_val, target_task.get("target_repo"))

    # insert_text rejects control chars, so collapse multi-line prompts to one
    # line (same normalization the HTTP client path documents). Somatic still
    # 400s here — backport this collapse office-side.
    clean_text = " ".join(prompt_text.replace("\r\n", "\n").split())
    return insert_text(clean_text)
