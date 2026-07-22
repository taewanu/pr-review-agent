"""Tests for daemon/lib.sh's run_with_timeout backstop (#76).

The helper is a shell function, so tests source lib.sh and call it via `bash -c`.
They pin the contract reply-pr.sh / review-pr.sh rely on: an under-cap command
passes its exit status through, an over-cap one is killed with $TIMEOUT_EXIT
(142) so the caller routes to the ADR 0005 failure path instead of parsing
truncated output.

The cap became a watchdog-enforced `date +%s` deadline in #251, replacing perl's
`alarm`, which counts kernel-timer time and so stops advancing across a suspend.
The suspend itself is not reproducible in a test. What is testable, and what the
rewrite owes beyond the contract above, is below: the kill reaches the command's
children, it escalates to SIGKILL against a tree that ignores TERM, and a command
that dies of its own signal is still reported as itself rather than as a cap.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"

TIMEOUT_EXIT = 142


def _run(snippet: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Source lib.sh and run a snippet that calls run_with_timeout.

    `env` is merged over the caller's, and reaches lib.sh before it is sourced,
    which is the only point at which the `readonly` dials can be overridden.
    """
    return subprocess.run(
        ["bash", "-c", f"source {LIB}; {snippet}"],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})} if env else None,
        # A regression in the kill path leaves the helper blocked in `wait`
        # forever, so bound it here: the point of these tests is that nothing
        # runs unbounded, and a hanging test would prove it by hanging CI.
        timeout=60,
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


def test_timeout_kills_the_commands_children(tmp_path):
    """The kill must reach the whole tree, not just the command we launched.

    `claude -p` runs its real work in child processes. Signalling only the direct
    child returns $TIMEOUT_EXIT to a caller that has already failed the PR over,
    while the work goes on consuming tokens unsupervised.
    """
    pidfile = tmp_path / "grandchild.pid"
    result = _run(f"run_with_timeout 1 sh -c 'sleep 30 & echo $! >{pidfile}; wait'")
    assert result.returncode == TIMEOUT_EXIT, result.stderr

    grandchild = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(grandchild, signal.SIGKILL)  # don't leak it out of the test run
    raise AssertionError(f"pid {grandchild} outlived the cap")


def test_timeout_escalates_to_sigkill_when_term_is_ignored():
    """A tree that ignores TERM must still die at the cap.

    That is the case the guard exists for: a process wedged past a wake may never
    act on a TERM, and returning while it still runs leaves it consuming tokens
    under a caller that has already failed the PR over. The command ignores TERM
    in the shell and re-spawns the sleep its children die on, so only the KILL
    ends it.
    """
    start = time.monotonic()
    result = _run(
        "run_with_timeout 1 sh -c 'trap \"\" TERM; while :; do sleep 0.2; done'",
        env={"TIMEOUT_KILL_GRACE": "2"},
    )
    elapsed = time.monotonic() - start
    assert result.returncode == TIMEOUT_EXIT, result.stderr
    # Past the cap plus the grace (it survived the TERM), but bounded by the KILL.
    assert 2.5 < elapsed < 12, f"took {elapsed:.1f}s; escalation did not bound it"


def test_grace_and_poll_dials_are_overridable():
    """Both dials are `readonly` at source time, so only a pre-source export wins.

    test_timeout_escalates_to_sigkill_when_term_is_ignored depends on this; a
    refactor to plain assignment would break the override silently.
    """
    result = _run(
        'printf "%s %s" "$TIMEOUT_KILL_GRACE" "$TIMEOUT_POLL_INTERVAL"',
        env={"TIMEOUT_KILL_GRACE": "2", "TIMEOUT_POLL_INTERVAL": "3"},
    )
    assert result.stdout == "2 3"


def test_signal_death_is_not_reported_as_a_timeout():
    """A command that dies of its own signal must surface as itself.

    This is the whole reason the cap is marked by a flag file rather than read
    off the exit status: 137 and 143 are the neighbours a cap kill is likeliest
    to be conflated with. Keying the timeout branch off an rc range instead would
    route a crashed `claude -p` into the timeout path in reply-pr.sh/review-pr.sh.
    """
    assert _run("run_with_timeout 5 sh -c 'kill -9 $$'").returncode == 137
    assert _run("run_with_timeout 5 sh -c 'kill -TERM $$'").returncode == 143
