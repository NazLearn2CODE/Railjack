"""Minimal trace store — slim port of sssf's tracer.py pattern (stdlib only).

Every event lands in SQLite (WAL) AS IT HAPPENS — a queryable mirror for live
observability.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  adw_id TEXT PRIMARY KEY, adw_name TEXT, request TEXT, status TEXT,
  started_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS phases (
  phase_id TEXT PRIMARY KEY, adw_id TEXT, seq INTEGER, name TEXT, kind TEXT,
  owner TEXT, status TEXT DEFAULT 'fail', attempt INTEGER DEFAULT 0, error TEXT,
  started_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, adw_id TEXT, phase_id TEXT, type TEXT, name TEXT,
  payload_json TEXT, tokens INTEGER, ts TEXT
);
CREATE TABLE IF NOT EXISTS gate_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT, adw_id TEXT, phase_id TEXT, gate TEXT,
  passed INTEGER, checks_json TEXT, ts TEXT
);
"""


class Tracer:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self.conn.executescript(SCHEMA)

    def session_start(self, adw_id: str, adw_name: str, request: str = "") -> None:
        # Upsert: a task can be ▶'d more than once (stop + restart). Reset the row so
        # re-dispatch doesn't hit the adw_id PRIMARY KEY conflict.
        self.conn.execute(
            "INSERT INTO sessions(adw_id,adw_name,request,status,started_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(adw_id) DO UPDATE SET "
            "adw_name=excluded.adw_name, request=excluded.request, "
            "status='running', started_at=excluded.started_at, ended_at=NULL",
            (adw_id, adw_name, request, "running", _now()),
        )

    def session_end(self, adw_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET status=?, ended_at=? WHERE adw_id=?",
            (status, _now(), adw_id),
        )

    def phase_start(self, adw_id: str, seq: int, name: str, kind: str, owner: str) -> str:
        pid = _id("phs")
        self.conn.execute(
            "INSERT INTO phases(phase_id,adw_id,seq,name,kind,owner,status,started_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (pid, adw_id, seq, name, kind, owner, "running", _now()),
        )
        return pid

    def phase_end(self, phase_id: str, status: str, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE phases SET status=?, error=?, ended_at=? WHERE phase_id=?",
            (status, error, _now(), phase_id),
        )

    def event(
        self,
        adw_id: str,
        phase_id: str,
        type: str,
        name: str,
        payload: dict | list | str | None = None,
        tokens: int = 0,
    ) -> str:
        eid = _id("evt")
        payload_str = payload if isinstance(payload, str) else json.dumps(payload or {})
        self.conn.execute(
            "INSERT INTO events(event_id,adw_id,phase_id,type,name,payload_json,tokens,ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (eid, adw_id, phase_id, type, name, payload_str, tokens, _now()),
        )
        return eid

    def gate(self, adw_id: str, phase_id: str, gate_name: str, passed: bool, checks: list | dict) -> None:
        self.conn.execute(
            "INSERT INTO gate_results(adw_id,phase_id,gate,passed,checks_json,ts) "
            "VALUES(?,?,?,?,?,?)",
            (adw_id, phase_id, gate_name, 1 if passed else 0, json.dumps(checks), _now()),
        )


_tracer_instance: Tracer | None = None
_tracer_db_path: str | Path | None = None


def get_default_db_path() -> Path:
    return Path(__file__).parent / "sssf.db"


def set_tracer_db_path(db_path: str | Path | None) -> None:
    global _tracer_instance, _tracer_db_path
    _tracer_db_path = db_path
    _tracer_instance = None


def get_tracer(db_path: str | Path | None = None) -> Tracer:
    global _tracer_instance
    target_path = db_path or _tracer_db_path or get_default_db_path()
    if _tracer_instance is None or _tracer_instance.db_path != str(target_path):
        _tracer_instance = Tracer(target_path)
    return _tracer_instance


def trace_for_task(task_id: int, db_path: str | Path | None = None) -> dict:
    target_path = str(db_path or _tracer_db_path or get_default_db_path())
    if not Path(target_path).exists():
        return {"session": None, "phases": [], "events": [], "gate_results": []}

    adw_id = f"task-{task_id}"
    conn = sqlite3.connect(target_path)
    conn.row_factory = sqlite3.Row
    try:
        s_row = conn.execute("SELECT * FROM sessions WHERE adw_id=?", (adw_id,)).fetchone()
        session = {k: s_row[k] for k in s_row.keys()} if s_row else None

        p_rows = conn.execute(
            "SELECT * FROM phases WHERE adw_id=? ORDER BY seq, started_at", (adw_id,)
        ).fetchall()
        phases = [{k: r[k] for k in r.keys()} for r in p_rows]

        e_rows = conn.execute(
            "SELECT * FROM events WHERE adw_id=? ORDER BY ts", (adw_id,)
        ).fetchall()
        events = []
        for r in e_rows:
            d = {k: r[k] for k in r.keys()}
            if d.get("payload_json"):
                try:
                    d["payload"] = json.loads(d["payload_json"])
                except Exception:
                    d["payload"] = d["payload_json"]
            events.append(d)

        g_rows = conn.execute(
            "SELECT * FROM gate_results WHERE adw_id=? ORDER BY ts", (adw_id,)
        ).fetchall()
        gate_results = []
        for r in g_rows:
            d = {k: r[k] for k in r.keys()}
            if d.get("checks_json"):
                try:
                    d["checks"] = json.loads(d["checks_json"])
                except Exception:
                    d["checks"] = d["checks_json"]
            gate_results.append(d)

        return {
            "session": session,
            "phases": phases,
            "events": events,
            "gate_results": gate_results,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    import tempfile

    tmp_dir = tempfile.mkdtemp()
    t = Tracer(Path(tmp_dir) / "selfcheck.db")
    a = "task-999"
    t.session_start(a, "selfcheck task", "task description")
    p = t.phase_start(a, 1, "worker", "agent", "claude")
    t.event(a, p, "dispatch", "claude -p", {"prompt_len": 42})
    t.phase_end(p, "pass")
    t.session_end(a, "accepted")

    res = trace_for_task(999, db_path=t.db_path)
    assert res["session"] is not None and res["session"]["adw_id"] == a, res
    assert len(res["phases"]) == 1, res
    assert len(res["events"]) == 1 and res["events"][0]["name"] == "claude -p", res
    print("tracer self-check OK (session, phase, event recorded & queried)")
