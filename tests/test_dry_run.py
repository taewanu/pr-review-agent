"""Tests for review-pr.sh's --dry-run reporting contract (emit_dryrun_contract, #209 A1a).

emit_dryrun_contract lives in lib.sh, not inline in review-pr.sh, so this test can
source it and assert the emitted fields, the ADR 0026 pattern used for
wait_for_lens_pids. The contract itself (the dryrun_*= fields and why) is
documented at the emitter in lib.sh.

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

# Generic scratch path standing in for the run-scoped global the caller sets.
PATHS = {"PAYLOAD_FILE": "/scratch/.pr-review-payload.json"}


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


def test_emits_payload_and_count_only():
    result = _emit(3)
    assert result.returncode == 0, result.stderr
    assert set(_parse(result.stdout)) == {"dryrun_payload", "dryrun_count"}


def test_payload_is_the_caller_global_verbatim():
    # The harness reads the full review payload from this exact path, so the value
    # must be the global unchanged.
    kv = _parse(_emit(3).stdout)
    assert kv["dryrun_payload"] == PATHS["PAYLOAD_FILE"]


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
