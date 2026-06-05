"""Tests for daemon/lib.sh's run_with_timeout backstop (#76).

The helper is a shell function, so tests source lib.sh and call it via `bash -c`.
They pin the contract reply-pr.sh / review-pr.sh rely on: an under-cap command
passes its exit status through, an over-cap one is killed with $TIMEOUT_EXIT
(142) so the caller routes to the ADR 0005 failure path instead of parsing
truncated output.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"

TIMEOUT_EXIT = 142


def _run(snippet: str) -> subprocess.CompletedProcess:
    """Source lib.sh and run a snippet that calls run_with_timeout."""
    return subprocess.run(
        ["bash", "-c", f"source {LIB}; {snippet}"],
        capture_output=True,
        text=True,
    )


def test_under_cap_passes_through_exit_and_stdout():
    result = _run("run_with_timeout 5 sh -c 'echo hello; exit 0'")
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"


def test_over_cap_is_killed_with_timeout_exit():
    start = time.monotonic()
    result = _run("run_with_timeout 1 sleep 10")
    elapsed = time.monotonic() - start
    assert result.returncode == TIMEOUT_EXIT, result.stderr
    # Killed near the cap, nowhere near the 10s the command wanted.
    assert elapsed < 5, f"took {elapsed:.1f}s; alarm did not fire"


def test_command_own_failure_is_not_masked_as_timeout():
    # An ordinary non-zero exit must surface as itself, not be misread as a timeout.
    result = _run("run_with_timeout 5 sh -c 'exit 3'")
    assert result.returncode == 3


def test_timeout_exit_constant_is_exported():
    result = _run('printf %s "$TIMEOUT_EXIT"')
    assert result.stdout == str(TIMEOUT_EXIT)
