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

import contextlib
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


def _pid_or_none(pidfile: Path) -> int | None:
    """The pid the nested command recorded, or None if it never wrote one."""
    text = pidfile.read_text().strip() if pidfile.exists() else ""
    return int(text) if text else None


def _read_pid(pidfile: Path) -> int:
    """The recorded pid; fail loudly if the inner tree never wrote one."""
    pid = _pid_or_none(pidfile)
    if pid is None:
        raise AssertionError(f"{pidfile.name} was never written: the inner tree never started")
    return pid


def _wait_until_gone(pid: int, timeout: float) -> bool:
    """Poll until `pid` exits, up to `timeout` seconds; True if it went."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def _kill_tree(pid: int | None) -> None:
    """Best-effort cleanup so a leaked process never escapes the test run.

    Signals the whole process group when it is safe: an orphan re-parents but
    keeps its group, so killpg reaches siblings a single os.kill would miss. It
    skips the group when it is our own, which a re-parented tree can share, since
    killing that would take the test runner down with it.
    """
    if pid is None:
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    if pgid != os.getpgid(0):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


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

    grandchild = _read_pid(pidfile)
    if _wait_until_gone(grandchild, timeout=10):
        return
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
    # Cap 2, not 1: at cap 1 a second-boundary read fires the watchdog with ~0
    # effective duration, so the KILL lands at just the grace (~2.3s) and trips
    # the lower bound below (#260, same degeneracy the nested-cap test hit). At
    # cap 2 the effective duration is at least ~1s, keeping elapsed above 3s.
    result = _run(
        "run_with_timeout 2 sh -c 'trap \"\" TERM; while :; do sleep 0.2; done'",
        env={"TIMEOUT_KILL_GRACE": "2"},
    )
    elapsed = time.monotonic() - start
    assert result.returncode == TIMEOUT_EXIT, result.stderr
    # Past the cap plus the grace (it survived the TERM), but bounded by the KILL.
    assert 2.5 < elapsed < 12, f"took {elapsed:.1f}s; escalation did not bound it"


def test_escalation_reaches_a_child_whose_parent_took_the_term(tmp_path):
    """A child that outlives its parent must still be killed.

    It re-parents to init the instant the parent dies, so it is no longer a
    descendant of the capped pid and a post-TERM walk cannot see it. Termination
    snapshots the tree before signalling for exactly this case. The parent also
    has to let the escalation finish: `wait` on the command returns as soon as
    the parent takes its TERM, which is long before the grace window that exists
    for what it left behind.
    """
    pidfile = tmp_path / "orphan.pid"
    stubborn = tmp_path / "stubborn.sh"
    stubborn.write_text("trap '' TERM\nsleep 60\n")
    parent = tmp_path / "parent.sh"
    # The parent dies on its TERM; the child ignores it and re-parents to init.
    parent.write_text(f"bash {stubborn} &\necho $! >{pidfile}\nwait\n")

    result = _run(
        f"run_with_timeout 1 bash {parent}",
        env={"TIMEOUT_KILL_GRACE": "2"},
    )
    assert result.returncode == TIMEOUT_EXIT, result.stderr

    orphan = _read_pid(pidfile)
    try:
        os.kill(orphan, 0)
    except ProcessLookupError:
        return
    os.kill(orphan, signal.SIGKILL)  # don't leak it out of the test run
    raise AssertionError(f"pid {orphan} survived: it re-parented out of the walk")


def _leaked_tree_report(deep_pid: int | None, marker: str) -> str:
    """Diagnostics for a nested-cap orphan: a ps snapshot plus which pid lived.

    Of the outer bash / inner bash / inner `sh -c` / `sleep 120` chain, which
    processes are still alive separates "snapshot taken before the tree existed"
    from "descendants re-parented after the TERM" (#260): the single fact the
    failure otherwise leaves inferred rather than observed.

    `marker` is the test's tmp_path; every process the nested call spawned
    carries it on its command line, so filtering ps to it isolates this test's
    tree from the rest of the suite run.
    """
    try:
        if deep_pid is None:
            verdict = "no pid was recorded (the inner tree never wrote one)"
        else:
            try:
                os.kill(deep_pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            verdict = f"pid {deep_pid} (sleep 120) is {'ALIVE' if alive else 'gone'}"

        snap = subprocess.run(
            ["ps", "-eo", "pid,ppid,pgid,stat,etime,command"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        rows = [
            line
            for line in snap.stdout.splitlines()
            if marker in line or line.split()[:1] == [str(deep_pid)]
        ]
        return "\n".join([verdict, *rows]) if rows else f"{verdict}\n(no matching ps rows)"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"pid {deep_pid}: ps unavailable ({exc})"


def test_a_nested_cap_does_not_orphan_the_inner_tree(tmp_path):
    """An outer cap must kill an inner one's work.

    The daemon nests this helper: run_with_pr_timeout wraps review-pr.sh, which
    calls it again per lens. Putting each job in its own process group would give
    the inner job a group the outer kill cannot reach, leaving a `claude -p` tree
    running with nothing supervising it.
    """
    pidfile = tmp_path / "deep.pid"
    inner_script = tmp_path / "inner.sh"
    inner_script.write_text(
        f"source {LIB}\nrun_with_timeout 300 sh -c 'sleep 120 & echo $! >{pidfile}; wait'\n"
    )

    # Outer cap 2, not 1: at cap 1 the watchdog's first `date +%s` read can land
    # at floor(T)+1 across a second boundary and fire with ~0 effective duration,
    # before the inner tree has forked the `sleep 120` for the outer kill to
    # reach (#260). Cap 2 keeps that first read below the deadline by
    # construction. stdout is detached so a leaked child cannot hold the capture
    # pipe open and turn this failure into a 60s hang.
    start = time.monotonic()
    # One finally covers both exits: the assertion path below and the 60s-hang
    # path, which is the one a leak is guaranteed on, so cleanup must reach it.
    try:
        try:
            result = subprocess.run(
                ["bash", "-c", f"source {LIB}; run_with_timeout 2 bash {inner_script}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(
                "run_with_timeout hung past 60s; a leaked child held it open\n"
                + _leaked_tree_report(_pid_or_none(pidfile), str(tmp_path))
            ) from exc
        elapsed = time.monotonic() - start
        assert result.returncode == TIMEOUT_EXIT, result.stderr

        deep = _read_pid(pidfile)
        if _wait_until_gone(deep, timeout=10):
            return
        raise AssertionError(
            f"pid {deep} outlived the outer cap after {elapsed:.1f}s\n"
            + _leaked_tree_report(deep, str(tmp_path))
        )
    finally:
        _kill_tree(_pid_or_none(pidfile))


def test_an_early_finishing_command_does_not_hold_a_pipe_open():
    """Every call site pipes into stream_format, so a held pipe is real latency.

    The watchdog outlives a command that finished early and inherits its stdout,
    so without redirecting it the reader blocks for the rest of the poll interval
    even though the command returned at once. #251's acceptance says the
    enforcement must not add latency.
    """
    start = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", f"source {LIB}; run_with_timeout 30 sh -c 'echo hello' | cat"],
        capture_output=True,
        text=True,
        env={**os.environ, "TIMEOUT_POLL_INTERVAL": "5"},
        timeout=60,
    )
    elapsed = time.monotonic() - start
    assert result.stdout.strip() == "hello"
    # Well under the 5s interval: the reader must not wait on the watchdog.
    assert elapsed < 2, f"pipe stayed open {elapsed:.1f}s; watchdog held the write end"


def test_the_job_stays_in_the_shells_process_group(tmp_path):
    """Ctrl-C must still stop a running review.

    A terminal sends SIGINT only to its foreground process group, and
    daemon/run.sh traps INT to exit without signalling descendants. Splitting the
    job into its own group (`set -m`) silently breaks the documented stop flow.
    """
    # The capped command reports its own group, so this exercises the real path;
    # backgrounding a sleep in the snippet would only measure bash's default.
    reporter = tmp_path / "pgid.sh"
    reporter.write_text("ps -o pgid= -p $$\n")
    result = _run(f'printf "%s " "$(ps -o pgid= -p $$)"; run_with_timeout 5 bash {reporter}')
    shell_pgid, job_pgid = result.stdout.split()
    assert shell_pgid == job_pgid, "job left the shell's group; Ctrl-C would miss it"


def test_stdin_redirected_into_the_helper_reaches_the_command(tmp_path):
    """Every lens feeds its prompt this way, and an empty one produces nothing.

    Callers redirect into the function, not the command: `run_with_timeout N
    claude ... <prompt` binds the file to run_with_timeout. Bash points an async
    command at /dev/null unless it is given an explicit redirect, so dropping the
    `0<&0` silently starves every `claude -p` of its prompt. The perl-exec helper
    inherited stdin, which is why no call site spells the redirect out.
    """
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("PROMPT-CONTENT\n")
    result = _run(f"run_with_timeout 5 cat <{prompt}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PROMPT-CONTENT", "the command read an empty stdin"


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
