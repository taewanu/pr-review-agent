"""Tests for the checks-row helpers in daemon/lib.sh (#308).

The review's state also lives on the PR's checks row: an `in_progress` run opened
before the multi-minute review, concluded on the verdict the status comment
renders. `start_check_run` / `complete_check_run` are the gh-backed create and
conclude, and `check_conclusion_for_state` is the verdict-to-conclusion mapping
both surfaces read from. The request bodies travel on stdin (`gh api --input -`),
so the stub here records stdin as well as argv, unlike test_status_comment's.

GitHub blocks a merge on a required check whose conclusion is `failure`,
`action_required`, `cancelled`, or `timed_out`, and lets `success`, `neutral`,
and `skipped` through; the mapping tests assert against that split rather than
against literal strings alone.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"
APP_STUB = REPO_ROOT / "tests" / "lib_app_stub.sh"

# Conclusions GitHub lets a merge through on, per its check-run docs.
NON_BLOCKING = {"success", "neutral", "skipped"}


def _run(
    call: str, *, gh_stdout: str = "", gh_exit: int = 0, env_extra: dict | None = None
) -> tuple[str, str, int, str, list[str]]:
    """Source lib.sh and run `call` with a stubbed `gh` that records both its
    argv and the request body piped to it. Returns (stdout, stderr, returncode,
    recorded argv, recorded stdin payloads)."""
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "gh_calls.log"
        bodies = Path(tmp) / "gh_bodies"
        bodies.mkdir()
        stub = Path(tmp) / "gh"
        out = gh_stdout.replace("'", "'\\''")
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{log}"\n'
            f'cat > "{bodies}/$$.json"\n'
            f"printf '%s' '{out}'\n"
            f"exit {gh_exit}\n"
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = os.environ.copy()
        env["PATH"] = f"{tmp}:{env['PATH']}"
        if env_extra:
            env.update(env_extra)
        result = subprocess.run(
            ["bash", "-c", f"source {LIB}; source {APP_STUB}; {call}"],
            capture_output=True,
            text=True,
            env=env,
        )
        calls = log.read_text() if log.exists() else ""
        payloads = [p.read_text() for p in sorted(bodies.iterdir())]
        return result.stdout, result.stderr, result.returncode, calls, payloads


# --- start_check_run --------------------------------------------------------


def test_start_check_run_opens_an_in_progress_run_and_prints_its_id():
    out, _, rc, calls, payloads = _run(
        "start_check_run owner repo deadbeef https://example.test/pr#issuecomment-1",
        gh_stdout="424242",
    )
    assert rc == 0
    assert out.strip() == "424242"
    assert "POST" in calls
    assert "repos/owner/repo/check-runs" in calls
    body = json.loads(payloads[0])
    assert body["status"] == "in_progress"
    assert body["head_sha"] == "deadbeef"
    # The name is what an operator types into branch protection to require the
    # review, so it must reach the API as the fixed string, not the App slug.
    assert body["name"] == "review"
    # The row's "Details" link lands on the status comment, the surface carrying
    # what the row omits.
    assert body["details_url"] == "https://example.test/pr#issuecomment-1"


def test_start_check_run_omits_details_url_when_there_is_none():
    # A status comment that failed to post leaves no link; the run must still
    # open rather than send an empty details_url the API would reject.
    _, _, rc, _, payloads = _run("start_check_run owner repo deadbeef ''", gh_stdout="1")
    assert rc == 0
    assert "details_url" not in json.loads(payloads[0])


def test_start_check_run_swallows_failure():
    # An installation without checks: write still gets its review; the row is
    # the part that degrades.
    out, _, rc, _, _ = _run("start_check_run owner repo deadbeef ''", gh_exit=1)
    assert rc == 0
    assert out.strip() == ""


# --- complete_check_run -----------------------------------------------------


def test_complete_check_run_concludes_the_run():
    _, _, rc, calls, payloads = _run(
        "complete_check_run owner repo 99 success 'No findings open' 'see the status comment'"
    )
    assert rc == 0
    assert "PATCH" in calls
    assert "repos/owner/repo/check-runs/99" in calls
    body = json.loads(payloads[0])
    assert body["status"] == "completed"
    assert body["conclusion"] == "success"
    assert body["output"]["title"] == "No findings open"
    assert body["output"]["summary"] == "see the status comment"


def test_complete_check_run_noop_on_empty_id():
    # No run was opened (the create degraded), so there is nothing to conclude.
    _, _, rc, calls, _ = _run("complete_check_run owner repo '' neutral title summary")
    assert rc == 0
    assert calls.strip() == ""


def test_complete_check_run_retries_then_gives_up_without_failing_the_review():
    _, stderr, rc, calls, _ = _run(
        "complete_check_run owner repo 99 failure title summary",
        gh_exit=1,
        env_extra={"CHECK_RUN_RETRY_SLEEP_SECONDS": "0"},
    )
    # A run left in_progress can hold a merge back, so the conclude is retried
    # before it is abandoned, and abandoning it is logged rather than silent.
    assert calls.count("PATCH") == 3
    assert rc == 0
    assert "not concluded after 3 attempts" in stderr
    assert "99" in stderr


def test_complete_check_run_absorbs_a_transient_failure():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "gh_calls.log"
        counter = Path(tmp) / "gh_attempts"
        stub = Path(tmp) / "gh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{log}"\n'
            "cat > /dev/null\n"
            f'n=$(cat "{counter}" 2>/dev/null || echo 0)\n'
            "n=$((n + 1))\n"
            f'echo "$n" > "{counter}"\n'
            'if [[ "$n" -lt 3 ]]; then exit 1; fi\n'
            "exit 0\n"
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = os.environ.copy()
        env["PATH"] = f"{tmp}:{env['PATH']}"
        env["CHECK_RUN_RETRY_SLEEP_SECONDS"] = "0"
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"source {LIB}; source {APP_STUB}; "
                "complete_check_run owner repo 99 success title summary",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        attempts = log.read_text().count("PATCH")
    assert result.returncode == 0
    assert attempts == 3
    assert "not concluded" not in result.stderr


# --- check_conclusion_for_state ---------------------------------------------


def _conclusion(state: str) -> str:
    out, _, rc, _, _ = _run(f"check_conclusion_for_state '{state}'")
    assert rc == 0
    return out.strip()


def test_a_clean_review_passes_the_check():
    assert _conclusion("pass") == "success"


def test_open_findings_fail_the_check_so_branch_protection_can_gate_on_it():
    # `neutral` would read as a pass to GitHub, which would settle the gating
    # question the operator's branch protection is supposed to settle.
    # `action_required` overstates a review whose findings may all be nits.
    assert _conclusion("block") == "failure"
    assert _conclusion("block") not in NON_BLOCKING
    assert _conclusion("block") != "action_required"


def test_a_review_with_no_verdict_never_gates_a_merge():
    # The crashed-review case: the trap concludes the run on a state the mapping
    # has never seen, and whatever it maps to must not hold a merge back over a
    # daemon-side failure the author cannot act on.
    for state in ("", "unknown", "reviewing"):
        assert _conclusion(state) == "neutral"
        assert _conclusion(state) in NON_BLOCKING


# --- review_state_for_open_threads (ADR 0040) --------------------------------


def _state(open_threads: str) -> str:
    out, _, rc, _, _ = _run(f"review_state_for_open_threads '{open_threads}'")
    assert rc == 0
    return out.strip()


def test_an_open_thread_blocks():
    assert _state("1") == "block"
    assert _conclusion(_state("1")) not in NON_BLOCKING


def test_no_open_thread_passes_however_the_tick_reached_that_state():
    # The gate has one input, so a review that posted findings with no thread left
    # open still passes. A finding nobody can resolve must not hold a merge, which
    # is the permanent-red state PR #312 hit.
    assert _state("0") == "pass"
    assert _conclusion(_state("0")) in NON_BLOCKING
