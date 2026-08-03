"""Pretty-printer for a task's SSSF trace — makes Phase A's data human-readable.

Usage:
  uv run python -m app.factory.trace_view <task_id>          # real db (app/factory/sssf.db)
  uv run python -m app.factory.trace_view <task_id> --db X   # alternate db (e.g. a test seed)

Reads via trace_for_task(); never writes. If a task has no trace (not dispatched, or
db absent) it says so plainly.
"""
from __future__ import annotations
import argparse
import json
import sys

from .tracer import trace_for_task


def print_trace(task_id: int, db_path: str | None = None) -> None:
    data = trace_for_task(task_id, db_path)
    s = data.get("session")
    if not s:
        print(f"(no trace for task {task_id})")
        return

    print(f"Task {task_id} — {s.get('adw_name', '?')}   [{s.get('status', '?')}]")
    print(f"  {s.get('started_at', '?')} → {s.get('ended_at') or '…'}")
    if s.get("request"):
        print(f"  request: {s['request'][:120]}{'…' if len(s['request']) > 120 else ''}")

    for ph in data.get("phases", []):
        print(
            f"  phase {ph.get('seq', '?')}: {ph.get('name', '?')} "
            f"({ph.get('kind', '?')}/{ph.get('owner', '?')})  "
            f"[{ph.get('status', '?')}]  {ph.get('started_at', '')}→{ph.get('ended_at') or '…'}"
        )
        if ph.get("error"):
            print(f"    ⚠ error: {ph['error']}")

    events = data.get("events", [])
    if events:
        print(f"  events ({len(events)}):")
        for e in events:
            tok = f"  · {e['tokens']}tok" if e.get("tokens") else ""
            payload = e.get("payload")
            pl = ""
            if payload:
                pl_s = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
                pl = f"  — {pl_s[:160]}{'…' if len(pl_s) > 160 else ''}"
            print(f"    {e.get('type', '?')}/{e.get('name', '?')}{pl}{tok}")

    gates = data.get("gate_results", [])
    if gates:
        print(f"  gates ({len(gates)}):")
        for g in gates:
            mark = "✓" if g.get("passed") else "✗"
            checks = g.get("checks")
            checks_s = json.dumps(checks, ensure_ascii=False) if checks is not None else ""
            print(f"    {mark} {g.get('gate', '?')}: {checks_s}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Pretty-print a task's SSSF trace.")
    p.add_argument("task_id", type=int, nargs="?", help="kanban task id (adw_id = task-<id>)")
    p.add_argument("--db", default=None, help="alternate trace db path")
    p.add_argument("--demo", action="store_true", help="seed a throwaway trace into a tmp db and print it")
    args = p.parse_args(argv)

    if args.demo or args.task_id is None:
        # ponytail: self-demo — seed a realistic trace into a tmp db so the viewer is
        # runnable with no live dispatch. Proves rendering end-to-end.
        import tempfile
        from pathlib import Path
        from .tracer import Tracer
        db = Path(tempfile.mkdtemp()) / "demo.db"
        t = Tracer(db)
        demo_id = 999
        a = f"task-{demo_id}"
        t.session_start(a, "demo: plan + build", "prove the trace viewer renders")
        pid = t.phase_start(a, 1, "worker", "agent", "claude")
        t.event(a, pid, "dispatch", "claude -p", {"prompt_len": 820})
        t.event(a, pid, "activity", "edited app/foo.py", tokens=1400)
        t.event(a, pid, "worker_exit", "0")
        t.phase_end(pid, "pass")
        t.session_end(a, "accepted")
        print("— demo trace (seeded) —")
        print_trace(999, db_path=str(db))
        return 0

    print_trace(args.task_id, db_path=args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
