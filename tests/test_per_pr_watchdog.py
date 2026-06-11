"""Tests for the per-PR network watchdog added for #121.

A stalled `git fetch` (laptop sleep / network drop) once froze the whole serial
poll loop for ~10h, since only the inner `claude -p` call was time-bounded. The
fix adds two defense-in-depth layers in `daemon/lib.sh`, both exercised here:

  - `run_with_pr_timeout` — the broad backstop poll.sh wraps each per-PR step in,
    so a hang in any network step (clone, fetch, gh api, claude) fails over to
    the next tick instead of wedging the loop.
  - `arm_git_stall_timeout` — exports git's low-speed thresholds so a stalled
    https clone/fetch self-aborts with a clean error in seconds, letting normal
    cleanup run rather than leaking an orphaned hang.

Both are shell functions, so tests source lib.sh and drive them via `bash -c`,
the same pattern as test_run_with_timeout.py / test_pr_lock.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"

TIMEOUT_EXIT = 142
# The inner `claude -p` cap (REVIEW_AGENT_TIMEOUT / REPLY_AGENT_TIMEOUT default).
# The outer per-PR cap must stay above it, or a legitimate slow review is killed.
INNER_AGENT_CAP = 300


def _run(snippet: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Source lib.sh and run a snippet, optionally with extra environment."""
    return subprocess.run(
        ["bash", "-c", f"source {LIB}; {snippet}"],
        capture_output=True,
        text=True,
        env={"PATH": __import__("os").environ["PATH"], **(env or {})},
    )


# --- PER_PR_TIMEOUT constant -------------------------------------------------


def test_per_pr_timeout_has_a_default():
    result = _run('printf %s "$PER_PR_TIMEOUT"')
    assert result.stdout.isdigit()
    assert int(result.stdout) == 600


def test_per_pr_timeout_exceeds_the_inner_agent_cap():
    # The outer watchdog must not kill a review that is legitimately running the
    # inner agent up to its own cap plus clone/fetch/post overhead.
    result = _run('printf %s "$PER_PR_TIMEOUT"')
    assert int(result.stdout) > INNER_AGENT_CAP


def test_per_pr_timeout_is_env_overridable():
    result = _run('printf %s "$PER_PR_TIMEOUT"', env={"PER_PR_TIMEOUT": "900"})
    assert result.stdout == "900"


# --- run_with_pr_timeout -----------------------------------------------------


def test_under_cap_passes_through_exit_and_stdout():
    result = _run("run_with_pr_timeout review-dispatch http://pr 0sha sh -c 'echo ok; exit 0'")
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"
    assert "failure:" not in result.stderr


def test_command_own_failure_is_passed_through_not_masked_as_timeout():
    # A per-PR step that fails on its own merits surfaces its real status, and is
    # NOT logged as a timeout failure.
    result = _run("run_with_pr_timeout review-dispatch http://pr 0sha sh -c 'exit 3'")
    assert result.returncode == 3
    assert "review-dispatch-timeout" not in result.stderr


def test_over_cap_is_killed_and_logged_as_a_structured_failure():
    result = _run(
        "run_with_pr_timeout review-dispatch http://pr/1 deadbeef sleep 10",
        env={"PER_PR_TIMEOUT": "1"},
    )
    assert result.returncode == TIMEOUT_EXIT, result.stderr
    # ADR 0005 positional failure line: category first, then the PR coordinates.
    assert "failure: review-dispatch-timeout pr=http://pr/1 sha=deadbeef" in result.stderr


# --- arm_git_stall_timeout ---------------------------------------------------


def test_arm_git_stall_timeout_exports_defaults():
    result = _run(
        'arm_git_stall_timeout; printf "%s %s" '
        '"$GIT_HTTP_LOW_SPEED_LIMIT" "$GIT_HTTP_LOW_SPEED_TIME"'
    )
    assert result.stdout == "1000 30"


def test_arm_git_stall_timeout_respects_pre_set_values():
    result = _run(
        'arm_git_stall_timeout; printf "%s %s" '
        '"$GIT_HTTP_LOW_SPEED_LIMIT" "$GIT_HTTP_LOW_SPEED_TIME"',
        env={"GIT_HTTP_LOW_SPEED_LIMIT": "500", "GIT_HTTP_LOW_SPEED_TIME": "10"},
    )
    assert result.stdout == "500 10"


def test_arm_git_stall_timeout_exports_to_child_processes():
    # The clone/fetch run as git child processes, so the thresholds must be
    # exported, not just set in the function's shell.
    result = _run("arm_git_stall_timeout; bash -c 'printf %s \"$GIT_HTTP_LOW_SPEED_TIME\"'")
    assert result.stdout == "30"
