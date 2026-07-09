"""Tests for daemon/lib.sh's global claude -p slot pool (ADR 0023 revision).

Every lens and the editor acquire one numbered slot before dispatch and
release it after, sharing the same noclobber + stale-reclaim mechanism as the
per-PR lock (test_pr_lock.py), extended to CLAUDE_SLOT_POOL_SIZE slots instead
of one. The slot files live on disk, so multiple concurrently-running
review-pr.sh processes (however many poll.sh's MAX_PARALLEL allows) share the
same pool automatically. Unlike acquire_pr_lock, acquire_claude_slot blocks: a
lens waits for a slot rather than skipping the PR on contention.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"


def _env(state_dir: Path, *, pool_size=None, stale=None, poll=None) -> dict:
    env = os.environ.copy()
    env["PR_REVIEW_STATE_DIR"] = str(state_dir)
    if pool_size is not None:
        env["CLAUDE_SLOT_POOL_SIZE"] = str(pool_size)
    if stale is not None:
        env["CLAUDE_SLOT_STALE_SECONDS"] = str(stale)
    if poll is not None:
        env["CLAUDE_SLOT_POLL_SECONDS"] = str(poll)
    return env


def _try_claim(state_dir: Path, slot: int, **kwargs) -> tuple[str, int]:
    result = subprocess.run(
        ["bash", "-c", f"source {LIB}; _try_claim_slot {slot}"],
        capture_output=True,
        text=True,
        env=_env(state_dir, **kwargs),
        timeout=10,
    )
    return result.stdout.strip(), result.returncode


def _acquire(state_dir: Path, *, timeout=10, **kwargs) -> tuple[str, int]:
    result = subprocess.run(
        ["bash", "-c", f"source {LIB}; acquire_claude_slot"],
        capture_output=True,
        text=True,
        env=_env(state_dir, **kwargs),
        timeout=timeout,
    )
    return result.stdout.strip(), result.returncode


def _acquire_with_label(state_dir: Path, label: str, *, timeout=10, **kwargs):
    return subprocess.run(
        ["bash", "-c", f"source {LIB}; acquire_claude_slot '{label}'"],
        capture_output=True,
        text=True,
        env=_env(state_dir, **kwargs),
        timeout=timeout,
    )


def _release(state_dir: Path, lock_path: str, **kwargs) -> int:
    return subprocess.run(
        ["bash", "-c", f"source {LIB}; release_claude_slot '{lock_path}'"],
        capture_output=True,
        text=True,
        env=_env(state_dir, **kwargs),
        timeout=10,
    ).returncode


def _slot_file(state_dir: Path, n: int) -> Path:
    return state_dir / f"claude-slot-{n}.lock"


def test_try_claim_when_free_succeeds(tmp_path: Path):
    out, rc = _try_claim(tmp_path, 1)
    assert rc == 0
    assert out == str(_slot_file(tmp_path, 1))
    assert _slot_file(tmp_path, 1).exists()


def test_try_claim_fails_when_held_by_live_process(tmp_path: Path):
    slot = _slot_file(tmp_path, 1)
    slot.write_text(f"{os.getpid()} {int(time.time())}\n")
    out, rc = _try_claim(tmp_path, 1)
    assert rc == 1
    assert out == ""


def test_try_claim_reclaims_from_dead_holder(tmp_path: Path):
    proc = subprocess.Popen(["true"])
    proc.wait()
    slot = _slot_file(tmp_path, 1)
    slot.write_text(f"{proc.pid} {int(time.time())}\n")
    out, rc = _try_claim(tmp_path, 1)
    assert rc == 0
    assert out == str(slot)
    assert not slot.read_text().startswith(f"{proc.pid} ")


def test_try_claim_reclaims_aged_out_lock(tmp_path: Path):
    slot = _slot_file(tmp_path, 1)
    slot.write_text(f"{os.getpid()} {int(time.time()) - 100}\n")
    out, rc = _try_claim(tmp_path, 1, stale=1)
    assert rc == 0
    assert out == str(slot)


def test_acquire_fills_lowest_numbered_free_slot_first(tmp_path: Path):
    out, rc = _acquire(tmp_path, pool_size=3)
    assert rc == 0
    assert out == str(_slot_file(tmp_path, 1))


def test_acquire_skips_held_slots_to_the_next_free_one(tmp_path: Path):
    _slot_file(tmp_path, 1).write_text(f"{os.getpid()} {int(time.time())}\n")
    out, rc = _acquire(tmp_path, pool_size=3)
    assert rc == 0
    assert out == str(_slot_file(tmp_path, 2))


def test_acquire_blocks_until_a_held_slot_is_released(tmp_path: Path):
    # Pool of 1, already held: acquire must wait, not fail immediately.
    only = _slot_file(tmp_path, 1)
    only.write_text(f"{os.getpid()} {int(time.time())}\n")

    def _release_after_delay():
        time.sleep(1.5)
        only.unlink()

    import threading

    t = threading.Thread(target=_release_after_delay)
    t.start()
    start = time.time()
    out, rc = _acquire(tmp_path, pool_size=1, poll=0.5, timeout=10)
    elapsed = time.time() - start
    t.join()
    assert rc == 0
    assert out == str(only)
    assert elapsed >= 1.0  # genuinely waited, did not return immediately


def test_release_removes_slot_and_allows_reacquire(tmp_path: Path):
    out, rc = _acquire(tmp_path, pool_size=1)
    assert rc == 0
    assert _release(tmp_path, out) == 0
    assert not _slot_file(tmp_path, 1).exists()
    _, rc2 = _acquire(tmp_path, pool_size=1)
    assert rc2 == 0


def test_release_is_noop_on_empty_path(tmp_path: Path):
    assert _release(tmp_path, "") == 0


def test_no_label_emits_no_log_line(tmp_path: Path):
    result = _acquire(tmp_path, pool_size=3)
    assert result[1] == 0
    assert result == (str(_slot_file(tmp_path, 1)), 0)


def test_label_logs_the_claimed_slot_and_pool_size(tmp_path: Path):
    # Dogfood follow-up: lens completion times varied 35-519s with no way to
    # tell whether a slow one was genuine complexity or slot contention. This
    # is the split: which slot, and how long the wait was.
    result = _acquire_with_label(tmp_path, "correctness lens", pool_size=3)
    assert result.returncode == 0
    assert result.stdout.strip() == str(_slot_file(tmp_path, 1))
    assert "correctness lens: acquired slot 1/3" in result.stderr


def test_label_omits_wait_suffix_on_immediate_acquire(tmp_path: Path):
    result = _acquire_with_label(tmp_path, "editor", pool_size=3)
    assert "waited" not in result.stderr


def test_label_reports_wait_time_when_it_actually_waited(tmp_path: Path):
    only = _slot_file(tmp_path, 1)
    only.write_text(f"{os.getpid()} {int(time.time())}\n")

    def _release_after_delay():
        time.sleep(1.5)
        only.unlink()

    import threading

    t = threading.Thread(target=_release_after_delay)
    t.start()
    result = _acquire_with_label(tmp_path, "tests lens", pool_size=1, poll=0.5, timeout=10)
    t.join()
    assert result.returncode == 0
    assert "tests lens: acquired slot 1/1 (waited" in result.stderr


def test_pool_never_exceeds_its_size_under_real_concurrency(tmp_path: Path):
    # Integration-style: N real concurrent workers, pool size K < N. At every
    # instant, at most K slots may exist on disk simultaneously; every worker
    # must eventually acquire and release cleanly (no leaked slot files).
    n_workers, pool_size, hold_seconds = 5, 2, 1
    script = f"""
    source {LIB}
    slot=$(CLAUDE_SLOT_POOL_SIZE={pool_size} CLAUDE_SLOT_POLL_SECONDS=0.2 \
      PR_REVIEW_STATE_DIR={tmp_path} acquire_claude_slot)
    sleep {hold_seconds}
    PR_REVIEW_STATE_DIR={tmp_path} release_claude_slot "$slot"
    """
    procs = [
        subprocess.Popen(["bash", "-c", script], env=os.environ.copy()) for _ in range(n_workers)
    ]
    max_seen = 0
    deadline = time.time() + 15
    while any(p.poll() is None for p in procs) and time.time() < deadline:
        held = len(list(tmp_path.glob("claude-slot-*.lock")))
        max_seen = max(max_seen, held)
        time.sleep(0.1)
    for p in procs:
        p.wait(timeout=5)
    assert max_seen <= pool_size
    assert max_seen > 0  # sanity: contention was actually observed at some point
    assert list(tmp_path.glob("claude-slot-*.lock")) == []  # nothing leaked


# ---------- pool-size validation (#200) ----------
# CLAUDE_SLOT_POOL_SIZE was a raw `${...:-3}` read: a typo'd 50 fanned out 50
# concurrent `claude -p` calls (the gap #161's ceiling closed for
# MAX_PARALLEL), and 0 or garbage spun the acquire loop forever. As a runtime
# env read inside a live review, it degrades with a warning instead of
# hard-failing, per the CONFIDENCE_THRESHOLD contract.


def _pool_size(state_dir: Path, raw: str) -> tuple[str, str]:
    result = subprocess.run(
        ["bash", "-c", f"source {LIB}; _slot_pool_size"],
        capture_output=True,
        text=True,
        env=_env(state_dir, pool_size=raw),
        timeout=10,
    )
    return result.stdout.strip(), result.stderr


def test_pool_size_valid_passes_through_silently(tmp_path: Path):
    size, err = _pool_size(tmp_path, "5")
    assert size == "5"
    assert err == ""


def test_pool_size_above_ceiling_is_clamped_with_warning(tmp_path: Path):
    size, err = _pool_size(tmp_path, "50")
    assert size == "16"
    assert "CLAUDE_SLOT_POOL_SIZE" in err


def test_pool_size_zero_falls_back_to_default(tmp_path: Path):
    # A zero pool would make every acquire spin forever: no slot 1..0 exists.
    size, err = _pool_size(tmp_path, "0")
    assert size == "3"
    assert "CLAUDE_SLOT_POOL_SIZE" in err


def test_pool_size_non_integer_falls_back_to_default(tmp_path: Path):
    size, err = _pool_size(tmp_path, "many")
    assert size == "3"
    assert "CLAUDE_SLOT_POOL_SIZE" in err


def test_acquire_with_zero_pool_size_still_acquires(tmp_path: Path):
    # The end-to-end payoff: before validation, CLAUDE_SLOT_POOL_SIZE=0 hung
    # acquire_claude_slot forever (caught here only by the harness timeout).
    out, rc = _acquire(tmp_path, pool_size=0)
    assert rc == 0
    assert out.startswith(str(tmp_path))
