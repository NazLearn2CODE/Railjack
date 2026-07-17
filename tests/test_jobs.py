"""Job state machine (``app/ffmpeg_jobs._execute``) with a mocked subprocess.

No pytest-asyncio: each case wraps ``_execute`` in ``asyncio.run``. Progress cap
(99 until done) is observed via the error path — a nonzero rc leaves the
mid-run ``progress`` value untouched, so we can read what the pump enforced.
"""

import asyncio
import signal

from app import ffmpeg_jobs
from app.ffmpeg_jobs import Job


class _FakeStream:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    def __init__(self, out_lines, err_lines, rc):
        self.stdout = _FakeStream(out_lines)
        self.stderr = _FakeStream(err_lines)
        self._rc = rc

    async def wait(self):
        return self._rc

    def send_signal(self, _sig):
        pass


def _run(monkeypatch, fake_exec, job, total_us):
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    async def _go():
        # ponytail: fresh Lock bound to THIS loop. The module-global Lock was
        # created at import (no loop); reused across asyncio.run's fresh loops
        # it would raise "bound to a different event loop". One job per test,
        # so a per-run lock is correct and avoids the cross-loop trip entirely.
        ffmpeg_jobs._LOCK = asyncio.Lock()
        await ffmpeg_jobs._execute(job, ["ffmpeg", "-y"], total_us)

    asyncio.run(_go())


def _execReturning(out=(), err=(), rc=0):
    async def fake_exec(*_a, **_k):
        return _FakeProc(list(out), list(err), rc)

    return fake_exec


def test_done_rc0_progress_100(monkeypatch):
    job = Job(id="t1", op="xfade")
    _run(
        monkeypatch,
        _execReturning(out=[b"out_time_us=5000000\n", b"out_time_us=9000000\n"], rc=0),
        job,
        10_000_000,
    )
    assert job.status == "done"
    assert job.progress == 100


def test_progress_capped_at_99(monkeypatch):
    job = Job(id="t2", op="xfade")
    _run(monkeypatch, _execReturning(out=[b"out_time_us=12000000\n"], rc=1), job, 10_000_000)
    assert job.status == "error"
    assert job.progress == 99  # 12s/10s = 120% -> clamped to 99, left by the error path


def test_progress_fraction(monkeypatch):
    job = Job(id="t3", op="xfade")
    _run(monkeypatch, _execReturning(out=[b"out_time_us=5000000\n"], rc=1), job, 10_000_000)
    assert job.progress == 50


def test_nonzero_rc_error_message(monkeypatch):
    job = Job(id="t4", op="concat")
    _run(monkeypatch, _execReturning(rc=2), job, 1)
    assert job.status == "error"
    assert job.error == "ffmpeg exited 2"


def test_sigterm_rc_cancelled(monkeypatch):
    job = Job(id="t5", op="xfade")
    _run(monkeypatch, _execReturning(rc=-signal.SIGTERM), job, 1)
    assert job.status == "cancelled"


def test_cancel_flag_cancelled(monkeypatch):
    job = Job(id="t6", op="xfade")
    job.cancel = True
    _run(monkeypatch, _execReturning(rc=0), job, 1)
    assert job.status == "cancelled"


def test_exec_not_found(monkeypatch):
    async def boom(*_a, **_k):
        raise FileNotFoundError

    job = Job(id="t7", op="xfade")
    _run(monkeypatch, boom, job, 1)
    assert job.status == "error"
    assert job.error == "ffmpeg binary not found on PATH"


def test_stderr_lines_land_in_logs(monkeypatch):
    job = Job(id="t8", op="xfade")
    _run(monkeypatch, _execReturning(err=[f"frame {i}\n".encode() for i in range(5)], rc=0), job, 1)
    assert list(job.logs) == [f"frame {i}" for i in range(5)]


def test_logs_deque_maxlen_200(monkeypatch):
    job = Job(id="t9", op="xfade")
    _run(monkeypatch, _execReturning(err=[b"line %d\n" % i for i in range(250)], rc=0), job, 1)
    assert len(job.logs) == 200
    assert list(job.logs)[-1] == "line 249"
