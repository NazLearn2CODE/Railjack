"""Railjack PROJECTS panel (PM module) backend.

Parses Cephalon-protocol markdown and git activity from project folders (e.g. ~/GameDev),
infers lifecycle phase, RAG health status, progress percentage, and GTD next actions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import yaml

from .config import CONFIG

logger = logging.getLogger(__name__)

router = APIRouter(tags=["projects"])

# Cache state: (timestamp, list of ProjectSummary dicts)
_CACHE: dict[str, Any] = {"timestamp": 0.0, "data": []}
_CACHE_LOCK = asyncio.Lock()

# ── Data Models ─────────────────────────────────────────────────────────────


class NextAction(BaseModel):
    text: str
    source_file: str
    quote: str


class LastCommit(BaseModel):
    hash: str
    date: str
    relative_date: str
    author: str
    message: str


class ProtocolStatus(BaseModel):
    index: bool
    codecompass_state: str  # "filled" | "template-unfilled" | "missing"
    gates: bool
    decisions_n: int
    sessions_n: int


class MilestoneItem(BaseModel):
    checked: bool
    name: str
    phase: str = ""
    date: str = ""
    commit: str = ""


class GateItem(BaseModel):
    name: str
    cmd: str


class DecisionItem(BaseModel):
    date: str
    title: str
    status: str
    filename: str


class SessionLogItem(BaseModel):
    date: str
    title: str
    filename: str
    snippet: str


class ProjectSummary(BaseModel):
    id: str
    name: str
    path: str
    phase: str
    health: str  # "green" | "amber" | "red" | "grey"
    trend: str
    trend_sparkline: list[int]  # 14-day daily commit counts
    pct: int | None
    pct_basis: str
    next_action: NextAction
    blockers: list[str]
    last_commit: LastCommit | None
    protocol: ProtocolStatus


class ProjectDetail(ProjectSummary):
    repo_type: str  # "game" | "tool"
    milestones: list[MilestoneItem] = []
    gates: list[GateItem] = []
    decisions: list[DecisionItem] = []
    session_logs: list[SessionLogItem] = []
    git_log: list[LastCommit] = []


# ── Config Access ────────────────────────────────────────────────────────────


def get_projects_options() -> dict[str, Any]:
    """Read options block for projects module dynamically from CONFIG."""
    for m in CONFIG.modules:
        if m.kind == "panel" and m.panel == "projects":
            return m.options or {}
    return {}


# ── Markdown & File Parsers ──────────────────────────────────────────────────


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and body from markdown content."""
    if not content.startswith("---"):
        return {}, content

    parts = re.split(r"^---\s*$", content, maxsplit=2, flags=re.MULTILINE)
    if len(parts) >= 3:
        fm_raw = parts[1]
        body = parts[2]
        try:
            parsed = yaml.safe_load(fm_raw)
            if isinstance(parsed, dict):
                return parsed, body
        except Exception as e:
            logger.debug("Failed to parse YAML frontmatter: %s", e)
    return {}, content


def parse_index_md(index_path: Path) -> dict[str, Any]:
    """Parse A-project/index.md for metadata, status prose, and milestone table."""
    result: dict[str, Any] = {
        "exists": False,
        "frontmatter": {},
        "working_on": None,
        "next_up": None,
        "last_build": None,
        "phase_prose": None,
        "milestones": [],
    }
    if not index_path.is_file():
        return result

    result["exists"] = True
    try:
        content = index_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("Error reading %s: %s", index_path, e)
        return result

    fm, body = parse_frontmatter(content)
    result["frontmatter"] = fm

    # Extract phase from prose if present: e.g. - **Phase:** ... current: **concept**
    m_phase = re.search(
        r"-\s*\*\*Phase:\*\*.*?current:\s*\*\*([^*]+)\*\*",
        content,
        re.IGNORECASE,
    )
    if not m_phase:
        m_phase = re.search(r"Current phase:\s*([^\n]+)", content, re.IGNORECASE)
    if m_phase:
        result["phase_prose"] = m_phase.group(1).strip()

    # Extract Current status block
    m_working = re.search(r"-\s*Working on:\s*([^\n]+)", content, re.IGNORECASE)
    if m_working:
        result["working_on"] = m_working.group(1).strip()

    m_next = re.search(r"-\s*Next up:\s*([^\n]+)", content, re.IGNORECASE)
    if m_next:
        result["next_up"] = m_next.group(1).strip()

    m_build = re.search(r"-\s*Last (?:build|deployed):\s*([^\n]+)", content, re.IGNORECASE)
    if m_build:
        result["last_build"] = m_build.group(1).strip()

    # Parse Milestone table
    milestones: list[MilestoneItem] = []
    lines = content.splitlines()
    in_table = False
    col_map: dict[str, int] = {}

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if in_table and stripped and not stripped.startswith("#"):
                # Non-empty, non-table line terminates table
                in_table = False
            continue

        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not cells:
            continue

        # Look for header row
        lower_cells = [c.lower() for c in cells]
        if not in_table:
            if any(
                k in lower_cells
                for k in ["milestone", "scope", "phase", "✅", "status"]
            ):
                in_table = True
                col_map = {}
                for idx, cell in enumerate(lower_cells):
                    if "✅" in cell or "status" in cell or cell == "done":
                        col_map["status"] = idx
                    elif "milestone" in cell or "scope" in cell or "task" in cell:
                        col_map["name"] = idx
                    elif "phase" in cell:
                        col_map["phase"] = idx
                    elif "date" in cell:
                        col_map["date"] = idx
                    elif "commit" in cell or "ref" in cell:
                        col_map["commit"] = idx
                continue

        # Skip separator line |---|---|
        if in_table and all(re.match(r"^:?-+:?$", c) for c in cells if c):
            continue

        if in_table and cells:
            status_idx = col_map.get("status", 0)
            name_idx = col_map.get("name", 1 if len(cells) > 1 else 0)
            phase_idx = col_map.get("phase", -1)
            date_idx = col_map.get("date", -1)
            commit_idx = col_map.get("commit", -1)

            status_val = cells[status_idx] if status_idx < len(cells) else ""
            name_val = cells[name_idx] if name_idx < len(cells) else ""
            phase_val = cells[phase_idx] if 0 <= phase_idx < len(cells) else ""
            date_val = cells[date_idx] if 0 <= date_idx < len(cells) else ""
            commit_val = cells[commit_idx] if 0 <= commit_idx < len(cells) else ""

            # Check if milestone is checked
            # Patterns: ✅, ✔, [x], [X], starts with ✅
            is_checked = bool(
                re.search(r"(?:✅|✔|\[[xX]\])", status_val)
                or status_val.lower().startswith("done")
                or status_val.lower().startswith("pass")
            )

            # Clean name (remove [ ] or ☐ if embedded in name)
            clean_name = re.sub(r"^\[[ xX]\]\s*", "", name_val).strip()

            if clean_name:
                milestones.append(
                    MilestoneItem(
                        checked=is_checked,
                        name=clean_name,
                        phase=phase_val,
                        date=date_val,
                        commit=commit_val,
                    )
                )

    result["milestones"] = milestones
    return result


def parse_codecompass(path: Path) -> tuple[str, str | None]:
    """Check CodeCompass.md state ('filled' | 'template-unfilled' | 'missing')
    and extract any explicit blocker string from 'Blocked:' line.
    """
    compass_file = path / "CodeCompass.md"
    if not compass_file.is_file():
        return "missing", None

    try:
        raw = compass_file.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("Error reading %s: %s", compass_file, e)
        return "missing", None

    # Check for template markers
    has_template_markers = bool(
        "{{PROJECT_NAME}}" in raw
        or "<!-- what's live in production" in raw
        or "<!-- One paragraph: the problem it solves" in raw
    )

    # Extract Blocked line
    m_blocked = re.search(r"\*\*Blocked:\*\*\s*(.*)", raw)
    blocked_line = m_blocked.group(1).strip() if m_blocked else ""

    # Remove HTML comments to see if there is real content
    clean_blocked = re.sub(r"<!--.*?-->", "", blocked_line, flags=re.DOTALL).strip()
    explicit_blocker = clean_blocked if clean_blocked else None

    # Check Current State section for real content
    m_state = re.search(r"## Current State\s*(.*?)(?=\n## |\Z)", raw, re.DOTALL)
    if m_state:
        state_body = m_state.group(1)
        state_no_comments = re.sub(r"<!--.*?-->", "", state_body, flags=re.DOTALL).strip()
        lines_with_content = [
            ln for ln in state_no_comments.splitlines()
            if re.sub(r"\*\*(?:Shipped|In progress|Blocked):\*\*", "", ln).strip()
        ]
        if not lines_with_content and has_template_markers:
            return "template-unfilled", explicit_blocker

    if has_template_markers:
        return "template-unfilled", explicit_blocker

    return "filled", explicit_blocker


def parse_gates(path: Path) -> tuple[bool, list[GateItem]]:
    """Parse gates.yml for named gate commands."""
    gates_file = path / "gates.yml"
    if not gates_file.is_file():
        return False, []

    try:
        content = gates_file.read_text(encoding="utf-8", errors="replace")
        data = yaml.safe_load(content)
        if isinstance(data, dict) and "gates" in data and isinstance(data["gates"], list):
            items = []
            for g in data["gates"]:
                if isinstance(g, dict):
                    items.append(
                        GateItem(
                            name=str(g.get("name", "")),
                            cmd=str(g.get("cmd", "")),
                        )
                    )
            return True, items
    except Exception as e:
        logger.warning("Error parsing %s: %s", gates_file, e)
    return True, []


def parse_decisions(path: Path) -> list[DecisionItem]:
    """Parse ADR markdown files in A-project/decisions/."""
    decisions_dir = path / "A-project" / "decisions"
    if not decisions_dir.is_dir():
        return []

    results: list[DecisionItem] = []
    for f in sorted(decisions_dir.glob("*.md"), reverse=True):
        if f.name == "template.md":
            continue

        date_str = ""
        # Match YYYY-MM-DD from filename
        m_date = re.match(r"^(\d{4}-\d{2}-\d{2})", f.name)
        if m_date:
            date_str = m_date.group(1)

        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            fm, _ = parse_frontmatter(content)
            title = str(fm.get("title", ""))
            status = str(fm.get("status", "proposed"))
            if not date_str and "created" in fm:
                date_str = str(fm["created"])

            if not title:
                # Extract first markdown header
                m_head = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
                title = m_head.group(1).strip() if m_head else f.stem

            results.append(
                DecisionItem(
                    date=date_str,
                    title=title,
                    status=status,
                    filename=f.name,
                )
            )
        except Exception as e:
            logger.debug("Failed parsing decision %s: %s", f, e)

    return results


def parse_sessions(
    path: Path,
) -> tuple[list[SessionLogItem], list[str], str | None, str | None, str | None]:
    """Parse B-sessions/ for logs, blockers, next_up candidate, and phase.

    Returns:
        (session_logs, blockers, next_up_text, next_up_source, next_up_quote)
    """
    sessions_dir = path / "B-sessions"
    blockers: list[str] = []
    session_logs: list[SessionLogItem] = []
    next_up_text: str | None = None
    next_up_source: str | None = None
    next_up_quote: str | None = None

    if not sessions_dir.is_dir():
        return [], [], None, None, None

    # First check hot.md if present (volatile now-cache)
    hot_file = sessions_dir / "hot.md"
    if hot_file.is_file():
        try:
            content = hot_file.read_text(encoding="utf-8", errors="replace")
            # Look for Handoffs / blockers
            m_blockers = re.search(
                r"##\s*(?:Handoffs\s*/\s*blockers|Blockers)\s*(.*?)(?=\n## |\Z)",
                content,
                re.DOTALL | re.IGNORECASE,
            )
            if m_blockers:
                for line in m_blockers.group(1).splitlines():
                    clean = line.strip()
                    if clean.startswith(("- ", "* ")):
                        item = clean[2:].strip()
                        if item and not item.startswith("Last build: none"):
                            blockers.append(item)

            # Look for Next up in hot.md
            m_next = re.search(
                r"##\s*Next up\s*(.*?)(?=\n## |\Z)",
                content,
                re.DOTALL | re.IGNORECASE,
            )
            if m_next:
                for line in m_next.group(1).splitlines():
                    clean = line.strip()
                    if clean.startswith(("- ", "* ")):
                        candidate = clean[2:].strip()
                        if candidate:
                            next_up_text = candidate
                            next_up_source = "B-sessions/hot.md"
                            next_up_quote = clean
                            break
        except Exception as e:
            logger.debug("Failed parsing hot.md in %s: %s", sessions_dir, e)

    # Now parse dated session logs
    session_files = sorted(
        [
            f for f in sessions_dir.glob("*.md")
            if f.name not in ("session-template.md", "hot.md")
        ],
        reverse=True,
    )

    for f in session_files:
        date_str = ""
        m_date = re.match(r"^(\d{4}-\d{2}-\d{2})", f.name)
        if m_date:
            date_str = m_date.group(1)

        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            fm, body = parse_frontmatter(content)
            title = str(fm.get("title", ""))
            if not title:
                m_head = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
                title = m_head.group(1).strip() if m_head else f.stem

            snippet = ""
            m_what = re.search(
                r"##\s*What was done\s*(.*?)(?=\n## |\Z)",
                body,
                re.DOTALL | re.IGNORECASE,
            )
            if m_what:
                lines = [ln.strip() for ln in m_what.group(1).splitlines() if ln.strip()]
                snippet = " ".join(lines[:3])[:200]

            session_logs.append(
                SessionLogItem(
                    date=date_str,
                    title=title,
                    filename=f.name,
                    snippet=snippet,
                )
            )

            # If next_up not found from hot.md, look in newest session log
            if next_up_text is None:
                m_next = re.search(
                    r"##\s*(?:Next up|Blockers\s*/\s*Next up|Next Steps)\s*(.*?)(?=\n## |\Z)",
                    body,
                    re.DOTALL | re.IGNORECASE,
                )
                if m_next:
                    for line in m_next.group(1).splitlines():
                        clean = line.strip()
                        if clean.startswith(("- ", "* ")):
                            c = clean[2:].strip()
                            if c.lower().startswith("next:"):
                                next_up_text = c[5:].strip()
                                next_up_source = f"B-sessions/{f.name}"
                                next_up_quote = clean
                                break
                            elif c.lower().startswith("next up:"):
                                next_up_text = c[8:].strip()
                                next_up_source = f"B-sessions/{f.name}"
                                next_up_quote = clean
                                break
                            elif c and not re.search(r"blocked|just not installed", c, re.I):
                                next_up_text = c
                                next_up_source = f"B-sessions/{f.name}"
                                next_up_quote = clean
                                break

            # Also extract blockers from session logs if not already captured
            m_b = re.search(
                r"##\s*(?:Blockers|Issues Found|Blockers\s*/\s*Next up)\s*(.*?)(?=\n## |\Z)",
                body,
                re.DOTALL | re.IGNORECASE,
            )
            if m_b:
                for line in m_b.group(1).splitlines():
                    clean = line.strip()
                    if clean.startswith(("- ", "* ")):
                        cand = clean[2:].strip()
                        if any(
                            kw in cand.lower()
                            for kw in ["blocked", "not installed", "waiting on", "needs ", "undecided"]
                        ):
                            if cand not in blockers:
                                blockers.append(cand)
        except Exception as e:
            logger.debug("Failed parsing session log %s: %s", f, e)

    return session_logs, blockers, next_up_text, next_up_source, next_up_quote


def parse_checklist(path: Path) -> tuple[int, int, str | None]:
    """Parse initial-checklist.md for checked/total and first unchecked item."""
    chk_file = path / "initial-checklist.md"
    if not chk_file.is_file():
        return 0, 0, None

    try:
        content = chk_file.read_text(encoding="utf-8", errors="replace")
        total = 0
        checked = 0
        first_unchecked: str | None = None
        for line in content.splitlines():
            m = re.match(r"^-\s*\[([ xX])\]\s*(.+)$", line.strip())
            if m:
                total += 1
                box = m.group(1)
                text = m.group(2).strip()
                if box in ("x", "X"):
                    checked += 1
                elif first_unchecked is None:
                    first_unchecked = text
        return checked, total, first_unchecked
    except Exception as e:
        logger.warning("Error reading %s: %s", chk_file, e)
        return 0, 0, None


def parse_brief_status(path: Path) -> tuple[str | None, str | None]:
    """Look for *-BRIEF.md at project root and extract Status: line."""
    for f in path.glob("*BRIEF*.md"):
        if f.is_file():
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"Status:\s*([^\n]+)", content, re.IGNORECASE)
                if m:
                    return m.group(1).strip(), f.name
            except Exception as e:
                logger.debug("Error reading brief %s: %s", f, e)
    return None, None


def is_archived_harvest(path: Path) -> bool:
    """Check Z-harvest/ for a filled harvest report (not just template)."""
    harvest_dir = path / "Z-harvest"
    if not harvest_dir.is_dir():
        return False
    reports = [
        f for f in harvest_dir.glob("*.md")
        if f.name != "harvest-report-template.md"
    ]
    return len(reports) > 0


# ── Git Activity Collector ───────────────────────────────────────────────────


async def get_git_activity(
    path: Path, days: int = 14
) -> tuple[LastCommit | None, str, list[int], int, bool, list[LastCommit]]:
    """Query git for last commit, 14-day sparkline, days idle, uncommitted changes, and commit log.

    Returns:
        (last_commit, trend_summary, sparkline_14d, days_since_commit, has_uncommitted, recent_commits)
    """
    git_dir = path / ".git"
    if not git_dir.exists():
        return None, "no git repo", [0] * days, 9999, False, []

    # 1. Check uncommitted changes
    has_uncommitted = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(path), "status", "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout.strip():
            has_uncommitted = True
    except Exception as e:
        logger.debug("git status failed on %s: %s", path, e)

    # 2. Get recent commits (up to 15)
    recent_commits: list[LastCommit] = []
    days_since_commit = 9999
    last_commit: LastCommit | None = None

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(path), "log", "-n", "15",
            "--pretty=format:%h%x00%an%x00%ct%x00%ar%x00%s",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            now_ts = time.time()
            lines = stdout.decode("utf-8", errors="replace").splitlines()
            for line in lines:
                parts = line.split("\x00")
                if len(parts) >= 5:
                    h, author, ts_str, rel_date, msg = parts[0], parts[1], parts[2], parts[3], parts[4]
                    try:
                        commit_ts = float(ts_str)
                        iso_date = datetime.fromtimestamp(commit_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        iso_date = rel_date
                        commit_ts = now_ts

                    item = LastCommit(
                        hash=h,
                        date=iso_date,
                        relative_date=rel_date,
                        author=author,
                        message=msg,
                    )
                    recent_commits.append(item)

            if recent_commits:
                last_commit = recent_commits[0]
                try:
                    first_ts = float(lines[0].split("\x00")[2])
                    days_since_commit = max(0, int((now_ts - first_ts) / 86400))
                except Exception:
                    days_since_commit = 0
    except Exception as e:
        logger.debug("git log failed on %s: %s", path, e)

    # 3. 14-day daily sparkline
    sparkline = [0] * days
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(path), "log", f"--since={days}.days",
            "--pretty=format:%ad", "--date=short",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            commit_dates = stdout.decode("utf-8", errors="replace").splitlines()
            # Map dates into buckets 0..(days-1) where (days-1) is today
            today = datetime.now(timezone.utc).date()
            for dt_str in commit_dates:
                dt_str = dt_str.strip()
                if not dt_str:
                    continue
                try:
                    c_date = datetime.strptime(dt_str, "%Y-%m-%d").date()
                    diff = (today - c_date).days
                    if 0 <= diff < days:
                        idx = (days - 1) - diff
                        sparkline[idx] += 1
                except Exception:
                    pass
    except Exception as e:
        logger.debug("git sparkline failed on %s: %s", path, e)

    total_commits_14d = sum(sparkline)
    if total_commits_14d > 0:
        trend_summary = f"{total_commits_14d} commit{'s' if total_commits_14d != 1 else ''} ({days}d)"
    elif days_since_commit < 9999:
        trend_summary = f"{days_since_commit}d idle"
    else:
        trend_summary = "idle"

    return last_commit, trend_summary, sparkline, days_since_commit, has_uncommitted, recent_commits


# ── Inference Engine ─────────────────────────────────────────────────────────


def infer_repo_type(path: Path) -> str:
    """Classify repo as 'game' or 'tool' per Decision 3."""
    # ~/GameDev/* is strictly game
    try:
        gamedev_root = Path("~/GameDev").expanduser().resolve()
        if path.resolve() == gamedev_root or gamedev_root in path.resolve().parents:
            return "game"
    except Exception:
        pass

    # Check game-specific protocol files
    if (path / "A-project" / "art-bible.md").exists():
        return "game"
    if (path / "A-project" / "gamedev-phases.md").exists():
        return "game"

    return "tool"


def infer_phase(
    repo_type: str,
    has_protocol: bool,
    is_archived: bool,
    index_fm: dict[str, Any],
    phase_prose: str | None,
    milestones: list[MilestoneItem],
    checklist_counts: tuple[int, int],
    brief_status: str | None,
    days_since_commit: int,
) -> str:
    """Infer project phase according to Decision 3."""
    if not has_protocol:
        return "no-protocol"

    if is_archived:
        return "archived"

    # Known phase keywords
    game_phases = [
        "concept",
        "pre-production",
        "production",
        "alpha",
        "beta",
        "gold",
        "live-ops",
    ]
    tool_phases = [
        "bootstrap",
        "brief/planning",
        "build",
        "harden/gates",
        "validated/shipped",
        "live-ops/maintenance",
    ]

    # 1. Frontmatter status
    fm_status = str(index_fm.get("status", "")).lower().strip()
    if repo_type == "game" and fm_status in game_phases:
        return fm_status
    if repo_type == "tool" and fm_status in tool_phases:
        return fm_status

    # 2. Prose phase in index.md / hot.md
    if phase_prose:
        norm_prose = phase_prose.lower().strip().strip("*").strip()
        if repo_type == "game" and norm_prose in game_phases:
            return norm_prose
        if repo_type == "tool" and norm_prose in tool_phases:
            return norm_prose

    # 3. BRIEF Status line
    if brief_status:
        b_upper = brief_status.upper()
        if "SHIPPED" in b_upper:
            return "live-ops" if repo_type == "game" else "validated/shipped"
        if "VALIDATOR FAIL" in b_upper:
            return "harden/gates"

    # 4. Milestone table position
    if milestones:
        for m in milestones:
            if not m.checked:
                if m.phase and m.phase.lower() in (game_phases if repo_type == "game" else tool_phases):
                    return m.phase.lower()
                break

    # 5. Checklist incomplete
    chk_checked, chk_total = checklist_counts
    if chk_total > 0 and chk_checked < chk_total:
        return "concept" if repo_type == "game" else "bootstrap"

    # 6. Default fallback
    if repo_type == "game":
        return "concept"
    return "build" if days_since_commit <= 14 else "live-ops/maintenance"


def infer_progress(
    phase: str,
    milestones: list[MilestoneItem],
    checklist_counts: tuple[int, int],
) -> tuple[int | None, str]:
    """Compute progress % and pct_basis per Decision 2."""
    if phase in ("no-protocol", "untracked"):
        return None, "no protocol"

    if phase == "archived":
        return 100, "archived (100%)"

    # Explicit milestone count
    if milestones:
        total = len(milestones)
        checked = sum(1 for m in milestones if m.checked)
        pct = round((checked / total) * 100) if total > 0 else 0
        return pct, f"milestones ({checked}/{total})"

    # Explicit checklist count
    chk_checked, chk_total = checklist_counts
    if chk_total > 0:
        pct = round((chk_checked / chk_total) * 100)
        return pct, f"checklist ({chk_checked}/{chk_total})"

    # Coarse bands labeled "est." (Decision 2)
    phase_lower = phase.lower()
    if phase_lower in ("concept", "bootstrap"):
        return 10, "0–25% (est.)"
    if phase_lower in ("pre-production", "brief/planning", "brief"):
        return 25, "25–50% (est.)"
    if phase_lower in ("production", "build"):
        return 50, "50–75% (est.)"
    if phase_lower in ("alpha", "beta", "harden/gates", "harden"):
        return 75, "75–99% (est.)"
    if phase_lower in ("gold", "validated/shipped", "shipped"):
        return 95, "75–99% (est.)"
    if phase_lower in ("live-ops", "live-ops/maintenance", "maintenance"):
        return 100, "live-ops (est.)"

    return None, "no estimate"


def infer_health(
    phase: str,
    days_since_commit: int,
    has_uncommitted: bool,
    codecompass_state: str,
    explicit_compass_blocker: str | None,
    blockers: list[str],
    brief_status: str | None,
) -> str:
    """Infer RAG health color per mechanical thresholds in §2."""
    if phase in ("no-protocol", "untracked", "archived"):
        return "grey"

    # RED thresholds:
    # 1. Explicit Blocked in CodeCompass
    if explicit_compass_blocker:
        return "red"

    # 2. Gate/validator FAIL in brief status
    if brief_status and "validator fail" in brief_status.lower():
        return "red"

    # 3. Stale > 30d while active in development
    if days_since_commit > 30 and phase in (
        "concept",
        "pre-production",
        "production",
        "build",
        "alpha",
    ):
        return "red"

    # AMBER thresholds:
    # 1. Stale 14-30d
    if 14 < days_since_commit <= 30:
        return "amber"

    # 2. CodeCompass unfilled template
    if codecompass_state == "template-unfilled":
        return "amber"

    # 3. Uncommitted working changes
    if has_uncommitted:
        return "amber"

    # 4. Dependency notes / blockers extracted
    if blockers:
        return "amber"

    # GREEN threshold:
    # commit <= 14d, no blockers, no failing gates
    if days_since_commit <= 14:
        return "green"

    return "grey"


def infer_next_action(
    phase: str,
    explicit_compass_blocker: str | None,
    brief_status: str | None,
    session_next_up: tuple[str | None, str | None, str | None],
    index_next_up: str | None,
    milestones: list[MilestoneItem],
    first_unchecked_chk: str | None,
    days_since_commit: int,
    last_commit: LastCommit | None,
) -> NextAction:
    """Priority ladder for next action resolution per §2."""
    # 1. Red blocker resolution
    if explicit_compass_blocker:
        return NextAction(
            text=f"Resolve blocker: {explicit_compass_blocker}",
            source_file="CodeCompass.md",
            quote=f"Blocked: {explicit_compass_blocker}",
        )

    # 2. Failing gate/validator
    if brief_status and "validator fail" in brief_status.lower():
        return NextAction(
            text="Fix failing validator / run gates.yml",
            source_file="*-BRIEF.md",
            quote=brief_status,
        )

    # 3. Newest session log "Next:" or index "Next up:"
    s_text, s_src, s_quote = session_next_up
    if s_text and s_src and s_quote:
        return NextAction(text=s_text, source_file=s_src, quote=s_quote)

    if index_next_up:
        return NextAction(
            text=index_next_up,
            source_file="A-project/index.md",
            quote=f"- Next up: {index_next_up}",
        )

    # 4. Milestone table -> first non-checked milestone
    for m in milestones:
        if not m.checked:
            return NextAction(
                text=m.name,
                source_file="A-project/index.md",
                quote=f"| ☐ | {m.name} | {m.phase} |",
            )

    # 5. initial-checklist.md -> first unchecked item
    if first_unchecked_chk:
        return NextAction(
            text=first_unchecked_chk,
            source_file="initial-checklist.md",
            quote=f"- [ ] {first_unchecked_chk}",
        )

    # 6. Fallbacks
    if phase in ("no-protocol", "untracked"):
        return NextAction(
            text="no protocol — stamp via project-bootstrap",
            source_file="(repo root)",
            quote="No A-project/ found",
        )

    if days_since_commit > 14:
        rel = last_commit.relative_date if last_commit else f"{days_since_commit}d ago"
        return NextAction(
            text=f"stale {days_since_commit}d — review & resume",
            source_file="(git)",
            quote=f"last commit was {rel}",
        )

    return NextAction(
        text="Review project status and plan next increment",
        source_file="A-project/index.md",
        quote="Project active",
    )


# ── Core Project Analysis ───────────────────────────────────────────────────


async def inspect_project(project_path: Path, days_window: int = 14) -> ProjectDetail:
    """Read and parse all signals for a single project path."""
    project_id = project_path.name
    repo_type = infer_repo_type(project_path)

    # Parse protocol files
    index_data = parse_index_md(project_path / "A-project" / "index.md")
    has_index = index_data["exists"]

    compass_state, compass_blocker = parse_codecompass(project_path)
    has_gates, gates_items = parse_gates(project_path)
    decisions = parse_decisions(project_path)
    (
        session_logs,
        session_blockers,
        session_next_text,
        session_next_src,
        session_next_quote,
    ) = parse_sessions(project_path)
    chk_checked, chk_total, first_unchecked_chk = parse_checklist(project_path)
    brief_status, _ = parse_brief_status(project_path)
    is_archived = is_archived_harvest(project_path)

    has_protocol = bool(
        has_index
        or has_gates
        or compass_state != "missing"
        or (project_path / "A-project").is_dir()
    )

    # Git activity
    (
        last_commit,
        trend_summary,
        sparkline,
        days_since_commit,
        has_uncommitted,
        recent_commits,
    ) = await get_git_activity(project_path, days=days_window)

    # Combine blockers
    blockers: list[str] = []
    if compass_blocker:
        blockers.append(compass_blocker)
    for b in session_blockers:
        if b not in blockers:
            blockers.append(b)

    # Project title / name
    fm = index_data.get("frontmatter", {})
    name = (
        str(fm.get("project") or fm.get("title") or "")
        or project_id
    )

    # Infer phase, progress %, health, and next action
    phase = infer_phase(
        repo_type=repo_type,
        has_protocol=has_protocol,
        is_archived=is_archived,
        index_fm=fm,
        phase_prose=index_data.get("phase_prose"),
        milestones=index_data.get("milestones", []),
        checklist_counts=(chk_checked, chk_total),
        brief_status=brief_status,
        days_since_commit=days_since_commit,
    )

    pct, pct_basis = infer_progress(
        phase=phase,
        milestones=index_data.get("milestones", []),
        checklist_counts=(chk_checked, chk_total),
    )

    health = infer_health(
        phase=phase,
        days_since_commit=days_since_commit,
        has_uncommitted=has_uncommitted,
        codecompass_state=compass_state,
        explicit_compass_blocker=compass_blocker,
        blockers=blockers,
        brief_status=brief_status,
    )

    next_action = infer_next_action(
        phase=phase,
        explicit_compass_blocker=compass_blocker,
        brief_status=brief_status,
        session_next_up=(session_next_text, session_next_src, session_next_quote),
        index_next_up=index_data.get("next_up"),
        milestones=index_data.get("milestones", []),
        first_unchecked_chk=first_unchecked_chk,
        days_since_commit=days_since_commit,
        last_commit=last_commit,
    )

    protocol_status = ProtocolStatus(
        index=has_index,
        codecompass_state=compass_state,
        gates=has_gates,
        decisions_n=len(decisions),
        sessions_n=len(session_logs),
    )

    return ProjectDetail(
        id=project_id,
        name=name,
        path=str(project_path.resolve()),
        phase=phase,
        health=health,
        trend=trend_summary,
        trend_sparkline=sparkline,
        pct=pct,
        pct_basis=pct_basis,
        next_action=next_action,
        blockers=blockers,
        last_commit=last_commit,
        protocol=protocol_status,
        repo_type=repo_type,
        milestones=index_data.get("milestones", []),
        gates=gates_items,
        decisions=decisions,
        session_logs=session_logs,
        git_log=recent_commits,
    )


def discover_project_paths() -> list[Path]:
    """Discover candidate project folders per configuration options."""
    options = get_projects_options()
    roots = options.get("roots", ["~/GameDev"])
    exclude_list = options.get("exclude", [])
    extra_projects = options.get("extra_projects", [])

    candidates: dict[str, Path] = {}

    for root_str in roots:
        root_p = Path(root_str).expanduser()
        if not root_p.is_dir():
            continue

        for child in sorted(root_p.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in exclude_list:
                continue

            # Candidate if has git or protocol markers
            if (
                (child / ".git").exists()
                or (child / "A-project").is_dir()
                or (child / "gates.yml").is_file()
                or (child / "CodeCompass.md").is_file()
                or child.name in extra_projects
                or str(child) in extra_projects
            ):
                candidates[str(child.resolve())] = child

    for extra_str in extra_projects:
        ep = Path(extra_str).expanduser()
        if ep.is_dir() and ep.name not in exclude_list:
            candidates[str(ep.resolve())] = ep

    # Return sorted deterministically by directory name
    return sorted(candidates.values(), key=lambda p: p.name.lower())


async def scan_all_projects(force: bool = False) -> list[ProjectDetail]:
    """Scan all discovered projects, using TTL cache unless force=True."""
    options = get_projects_options()
    ttl_s = options.get("cache_ttl_s", 300)
    days_window = options.get("git_log_days", 14)

    async with _CACHE_LOCK:
        now = time.time()
        if not force and _CACHE["data"] and (now - _CACHE["timestamp"] < ttl_s):
            return _CACHE["data"]

        paths = discover_project_paths()
        tasks = [inspect_project(p, days_window=days_window) for p in paths]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid_results: list[ProjectDetail] = []
        for r in results:
            if isinstance(r, ProjectDetail):
                valid_results.append(r)
            else:
                logger.error("Project inspection failed: %s", r)

        _CACHE["timestamp"] = now
        _CACHE["data"] = valid_results
        return valid_results


# ── HTTP Endpoints ───────────────────────────────────────────────────────────


@router.get("/api/projects/summary", response_model=list[ProjectSummary])
async def get_projects_summary() -> list[ProjectSummary]:
    """Portfolio overview: list of project status summaries."""
    details = await scan_all_projects(force=False)
    return [
        ProjectSummary(
            id=d.id,
            name=d.name,
            path=d.path,
            phase=d.phase,
            health=d.health,
            trend=d.trend,
            trend_sparkline=d.trend_sparkline,
            pct=d.pct,
            pct_basis=d.pct_basis,
            next_action=d.next_action,
            blockers=d.blockers,
            last_commit=d.last_commit,
            protocol=d.protocol,
        )
        for d in details
    ]


@router.get("/api/projects/{project_id}", response_model=ProjectDetail)
async def get_project_detail(project_id: str) -> ProjectDetail:
    """Detailed status, milestones, gates, decisions, sessions, and git log for a project."""
    details = await scan_all_projects(force=False)
    for d in details:
        if d.id == project_id:
            return d

    # If not in cache, check if directory exists directly in roots
    options = get_projects_options()
    for root_str in options.get("roots", ["~/GameDev"]):
        cand = Path(root_str).expanduser() / project_id
        if cand.is_dir():
            return await inspect_project(cand, days_window=options.get("git_log_days", 14))

    raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")


@router.post("/api/projects/rescan")
async def post_projects_rescan() -> dict[str, Any]:
    """Force re-parse all projects, bypassing cache TTL."""
    details = await scan_all_projects(force=True)
    summary = [
        ProjectSummary(
            id=d.id,
            name=d.name,
            path=d.path,
            phase=d.phase,
            health=d.health,
            trend=d.trend,
            trend_sparkline=d.trend_sparkline,
            pct=d.pct,
            pct_basis=d.pct_basis,
            next_action=d.next_action,
            blockers=d.blockers,
            last_commit=d.last_commit,
            protocol=d.protocol,
        )
        for d in details
    ]
    return {
        "status": "ok",
        "count": len(details),
        "projects": [s.model_dump() for s in summary],
    }
