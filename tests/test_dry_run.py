"""Tests for review-pr.sh's --dry-run reporting contract (emit_dryrun_contract, #209 A1a).

--dry-run runs the full generation pipeline but posts nothing, reporting where the
findings that would post live so the eval harness can read them. The contract is
machine-readable `dryrun_*=<path|count>` lines on stdout, matching the repo's
`key=value` signal convention (category=/truncated_count=). emit_dryrun_contract
lives in lib.sh, not inline in review-pr.sh, so this test can source it and assert
the emitted fields, the ADR 0026 pattern used for wait_for_lens_pids.

The no-post guards themselves (skipping the status comment, review object, and
resolution when DRY_RUN=1) are exercised by a manual dry-run smoke against a real
PR, not this unit test: driving the full script needs gh/claude and a clone.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"

# Generic scratch paths standing in for the run-scoped globals the caller sets.
PATHS = {
    "PAYLOAD_FILE": "/scratch/.pr-review-payload.json",
    "SUMMARY_FILE": "/scratch/.pr-review-summary.txt",
    "ANCHORED_FILE": "/scratch/.pr-review-anchored.json",
    "UNANCHORED_FILE": "/scratch/.pr-review-unanchored.json",
}


def _emit(count: int) -> subprocess.CompletedProcess:
    """Source lib.sh, set the payload-path globals, run emit_dryrun_contract."""
    setup = "; ".join(f"{k}={v}" for k, v in PATHS.items())
    return subprocess.run(
        ["bash", "-c", f"source {LIB}; {setup}; emit_dryrun_contract {count}"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
    )


def _parse(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


def test_emits_all_five_contract_keys():
    result = _emit(3)
    assert result.returncode == 0, result.stderr
    assert set(_parse(result.stdout)) == {
        "dryrun_payload",
        "dryrun_summary",
        "dryrun_anchored",
        "dryrun_unanchored",
        "dryrun_count",
    }


def test_paths_are_the_caller_globals_verbatim():
    # The harness relies on these paths resolving to the exact scratch files, so
    # the values must be the globals unchanged.
    kv = _parse(_emit(3).stdout)
    assert kv["dryrun_payload"] == PATHS["PAYLOAD_FILE"]
    assert kv["dryrun_summary"] == PATHS["SUMMARY_FILE"]
    assert kv["dryrun_anchored"] == PATHS["ANCHORED_FILE"]
    assert kv["dryrun_unanchored"] == PATHS["UNANCHORED_FILE"]


def test_count_is_reported_verbatim():
    assert _parse(_emit(7).stdout)["dryrun_count"] == "7"


def test_zero_count_is_a_first_class_result():
    # A dry-run that finds nothing must still report a zero count (a recall miss
    # the harness records), not stay silent and read as an errored run.
    assert _parse(_emit(0).stdout)["dryrun_count"] == "0"


def test_contract_is_stdout_only():
    # The harness parses stdout; the contract must not leak onto stderr.
    result = _emit(2)
    assert "dryrun_" not in result.stderr
