"""Tests for the run.sh daemon singleton + heartbeat (ADR 0009).

run.sh is the polling loop that replaces the launchd StartInterval timer (#83).
The singleton (acquire_daemon_singleton / release_daemon_singleton) stops two
loops from both driving ticks; the heartbeat (write_heartbeat) makes liveness
observable. Same noclobber + dead-holder-reclaim mechanism as the per-PR lock
(test_pr_lock.py), minus the stale window — a loop is long-lived by design, so
only a dead holder is reclaimable, never an aged-out live one.

The helpers touch only the filesystem and `kill -0`, so tests source lib.sh
directly with `PR_REVIEW_STATE_DIR` at a tmp dir. run.sh itself is exercised for
the singleton-refusal path so a second loop never starts behind a live one.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"
RUN = REPO_ROOT / "daemon" / "run.sh"


def _call(state_dir: Path, snippet: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PR_REVIEW_STATE_DIR"] = str(state_dir)
    return subprocess.run(
        ["bash", "-c", f"source {LIB}; {snippet}"],
        capture_output=True,
        text=True,
        env=env,
    )


def _pidfile(state_dir: Path) -> Path:
    return state_dir / "daemon.pid"


def _heartbeat(state_dir: Path) -> Path:
    return state_dir / "daemon.heartbeat"


def test_acquire_when_free_succeeds_and_creates_pidfile(tmp_path: Path):
    r = _call(tmp_path, "acquire_daemon_singleton")
    assert r.returncode == 0
    assert r.stdout.strip() == str(_pidfile(tmp_path))
    assert _pidfile(tmp_path).exists()


def test_acquire_fails_when_held_by_live_process(tmp_path: Path):
    # The pytest process is alive, so a pidfile claiming its PID is a live hold
    # and must not be stolen — this is the "no second loop" guarantee.
    _pidfile(tmp_path).write_text(f"{os.getpid()}\n")
    r = _call(tmp_path, "acquire_daemon_singleton")
    assert r.returncode == 1
    assert r.stdout.strip() == ""
    assert _pidfile(tmp_path).read_text().startswith(str(os.getpid()))


def test_reclaims_from_dead_holder(tmp_path: Path):
    # A reaped child's PID is gone, so its pidfile is abandoned (e.g. the prior
    # loop was SIGKILLed and never ran its cleanup trap).
    proc = subprocess.Popen(["true"])
    proc.wait()
    _pidfile(tmp_path).write_text(f"{proc.pid}\n")
    r = _call(tmp_path, "acquire_daemon_singleton")
    assert r.returncode == 0
    assert not _pidfile(tmp_path).read_text().startswith(f"{proc.pid}\n")


def test_empty_pidfile_treated_as_held(tmp_path: Path):
    # A pidfile mid-write (no parseable PID) is not reclaimable — no stale window
    # to age it out, so it stays held until its writer finishes or dies.
    _pidfile(tmp_path).write_text("\n")
    r = _call(tmp_path, "acquire_daemon_singleton")
    assert r.returncode == 1


def test_release_removes_pidfile_and_allows_reacquire(tmp_path: Path):
    r = _call(tmp_path, "acquire_daemon_singleton")
    assert r.returncode == 0
    rel = _call(tmp_path, f"release_daemon_singleton '{_pidfile(tmp_path)}'")
    assert rel.returncode == 0
    assert not _pidfile(tmp_path).exists()
    assert _call(tmp_path, "acquire_daemon_singleton").returncode == 0


def test_release_is_noop_on_empty_path(tmp_path: Path):
    # The cleanup trap calls release unconditionally, so an empty path (run.sh
    # exited before acquiring) must be a clean no-op.
    assert _call(tmp_path, "release_daemon_singleton ''").returncode == 0


def test_write_heartbeat_stamps_current_epoch(tmp_path: Path):
    before = int(time.time())
    r = _call(tmp_path, "write_heartbeat")
    assert r.returncode == 0
    hb = _heartbeat(tmp_path)
    assert hb.exists()
    stamped = int(hb.read_text().strip())
    assert before <= stamped <= int(time.time()) + 1


def test_run_sh_refuses_to_start_a_second_loop(tmp_path: Path):
    # A live holder means a loop is already running; run.sh must exit 0 without
    # starting a second loop (which would double every tick) and without
    # disturbing the live holder's pidfile.
    _pidfile(tmp_path).write_text(f"{os.getpid()}\n")
    env = os.environ.copy()
    env["PR_REVIEW_STATE_DIR"] = str(tmp_path)
    env["POLL_INTERVAL_SECONDS"] = "1"
    r = subprocess.run(
        ["bash", str(RUN)],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert r.returncode == 0
    assert "already running" in r.stderr
    # Untouched: the live holder still owns the pidfile.
    assert _pidfile(tmp_path).read_text().startswith(str(os.getpid()))
