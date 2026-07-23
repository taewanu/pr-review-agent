"""Tests for daemon/lib.sh's wait_for_lens_pids (ADR 0026).

Exercises the exact case pr-review-agent's self-review of PR #192 caught: the
lens wait loop used to `exit 1` on the first timed-out or empty-output lens,
leaving later-dispatched, still-running lenses unwaited. wait_for_lens_pids
must always reach the end of lens_pids, even when an earlier lens's outcome
is bad, so a still-running sibling lens is never orphaned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"


def _run(script: str, *, cwd: Path, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"source {LIB}; {script}"],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )


def test_waits_past_an_early_timeout_to_reap_a_slower_lens(tmp_path):
    slow_marker = tmp_path / "slow.raw"
    fast_raw = tmp_path / "fast.raw"
    fast_raw.write_text("")  # empty, as a timed-out lens's raw file would be

    script = f"""
REVIEW_AGENT_TIMEOUT=600
LENS_LABELS=(fast slow)
LENS_RAW_FILES=("{fast_raw}" "{slow_marker}")
lens_count=2
lens_pids=()

( exit $TIMEOUT_EXIT ) &
lens_pids[0]=$!

( sleep 0.3; echo done > "{slow_marker}" ) &
lens_pids[1]=$!

wait_for_lens_pids
echo "reached_end"
"""
    result = _run(script, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "reached_end" in result.stdout
    assert slow_marker.read_text() == "done\n"
    assert "fast lens exceeded 600s" in result.stderr


def test_logs_empty_output_without_aborting(tmp_path):
    empty_raw = tmp_path / "empty.raw"
    empty_raw.write_text("")
    ok_raw = tmp_path / "ok.raw"

    script = f"""
REVIEW_AGENT_TIMEOUT=600
LENS_LABELS=(security tests)
LENS_RAW_FILES=("{empty_raw}" "{ok_raw}")
lens_count=2
lens_pids=()

( exit 0 ) &
lens_pids[0]=$!

( echo findings > "{ok_raw}" ) &
lens_pids[1]=$!

wait_for_lens_pids
echo "reached_end"
"""
    result = _run(script, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "reached_end" in result.stdout
    assert ok_raw.read_text() == "findings\n"
    assert "security lens produced no output" in result.stderr


def test_all_lenses_succeed_no_log_noise(tmp_path):
    raw_a = tmp_path / "a.raw"
    raw_b = tmp_path / "b.raw"

    script = f"""
REVIEW_AGENT_TIMEOUT=600
LENS_LABELS=(general perf)
LENS_RAW_FILES=("{raw_a}" "{raw_b}")
lens_count=2
lens_pids=()

( echo a > "{raw_a}" ) &
lens_pids[0]=$!

( echo b > "{raw_b}" ) &
lens_pids[1]=$!

wait_for_lens_pids
echo "reached_end"
"""
    result = _run(script, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "reached_end" in result.stdout
    assert "exceeded" not in result.stderr
    assert "produced no output" not in result.stderr
