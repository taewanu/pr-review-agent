"""Tests for daemon/lib.sh's per-PR review lock (#67).

ADR 0008's own-PR auto-submit dropped the implicit "one pending review per PR"
constraint that used to serialize concurrent reviews. `acquire_pr_lock` /
`release_pr_lock` reinstate serialization locally with a noclobber lockfile
(macOS has no `flock`). The lock is held for the life of the review process, so
the load-bearing guarantee is "a lock held by a live process cannot be
acquired"; a lock whose holder has died, or that has outlived the stale window,
is reclaimable.

The helpers touch only the filesystem, `date`, and `kill -0`, so tests source
lib.sh directly with `PR_REVIEW_STATE_DIR` pointed at a tmp dir. The lockfile
holds "<holder-pid> <epoch>"; tests synthesize it to control the held/stale
state deterministically (a real subprocess holder would exit before the next
acquire could observe it, masking the held case behind dead-PID reclaim).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"


def _acquire(
    state_dir: Path, *, stale: int | None = None, owner="example", repo="example", pr="1"
) -> tuple[str, int]:
    env = os.environ.copy()
    env["PR_REVIEW_STATE_DIR"] = str(state_dir)
    if stale is not None:
        env["PR_REVIEW_LOCK_STALE_SECONDS"] = str(stale)
    result = subprocess.run(
        ["bash", "-c", f"source {LIB}; acquire_pr_lock {owner} {repo} {pr}"],
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip(), result.returncode


def _release(state_dir: Path, lock_path: str) -> int:
    env = os.environ.copy()
    env["PR_REVIEW_STATE_DIR"] = str(state_dir)
    return subprocess.run(
        ["bash", "-c", f"source {LIB}; release_pr_lock '{lock_path}'"],
        capture_output=True,
        text=True,
        env=env,
    ).returncode


def _lock_file(state_dir: Path, owner="example", repo="example", pr="1") -> Path:
    return state_dir / f"{owner}-{repo}-{pr}.lock"


def test_acquire_when_free_succeeds_and_creates_lockfile(tmp_path: Path):
    out, rc = _acquire(tmp_path)
    assert rc == 0
    assert out == str(_lock_file(tmp_path))
    assert _lock_file(tmp_path).exists()


def test_acquire_fails_when_held_by_live_process(tmp_path: Path):
    # The pytest process is alive, so a lock claiming its PID is a live hold and
    # must not be stolen. This is the concurrency guarantee: a review in flight
    # blocks a second one.
    lock = _lock_file(tmp_path)
    lock.write_text(f"{os.getpid()} {int(time.time())}\n")
    out, rc = _acquire(tmp_path)
    assert rc == 1
    assert out == ""
    # Untouched: still the original holder.
    assert lock.read_text().startswith(f"{os.getpid()} ")


def test_reclaims_lock_from_dead_holder(tmp_path: Path):
    # A reaped child's PID is no longer running, so its lock is abandoned.
    proc = subprocess.Popen(["true"])
    proc.wait()
    lock = _lock_file(tmp_path)
    lock.write_text(f"{proc.pid} {int(time.time())}\n")
    out, rc = _acquire(tmp_path)
    assert rc == 0
    assert out == str(lock)
    # Reclaimed: now held by the acquiring shell, not the dead PID.
    assert not lock.read_text().startswith(f"{proc.pid} ")


def test_reclaims_lock_when_aged_out(tmp_path: Path):
    # Live holder PID but the lock predates the stale window: treat as abandoned
    # (a review can't legitimately run longer than the window).
    lock = _lock_file(tmp_path)
    lock.write_text(f"{os.getpid()} {int(time.time()) - 100}\n")
    out, rc = _acquire(tmp_path, stale=1)
    assert rc == 0
    assert out == str(lock)


def test_partial_lock_is_treated_as_held(tmp_path: Path):
    # A lockfile with no parseable PID/epoch is a lock mid-acquisition by another
    # run; it must not be stolen.
    lock = _lock_file(tmp_path)
    lock.write_text("\n")
    _, rc = _acquire(tmp_path)
    assert rc == 1


def test_release_removes_lock_and_allows_reacquire(tmp_path: Path):
    out, rc = _acquire(tmp_path)
    assert rc == 0
    assert _release(tmp_path, out) == 0
    assert not _lock_file(tmp_path).exists()
    # Re-acquire after release succeeds.
    _, rc2 = _acquire(tmp_path)
    assert rc2 == 0


def test_release_is_noop_on_empty_path(tmp_path: Path):
    # cleanup() calls release unconditionally, even when the run exited before
    # acquiring, so an empty path must be a clean no-op.
    assert _release(tmp_path, "") == 0
