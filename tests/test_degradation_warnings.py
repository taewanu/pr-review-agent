"""Tests for lib.sh's `log_degradation_warnings` (#196).

review-pr.sh captures merge_findings.py stderr into a file and, before this
fix, read it only on the failure branch. The quality-degradation warnings
(merge-skip / finding-skip / confidence-gate) are emitted on the SUCCESS path,
so a lens silently dropping every run never reached the daemon log. These
tests drive the real chain: a malformed lens payload through merge_findings.py,
its captured stderr through the bash forwarder, into log output.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"
MERGE = REPO_ROOT / "daemon" / "merge_findings.py"


def _wrap(payload: dict) -> str:
    return f"lens prose\n\n```json\n{json.dumps(payload)}\n```\n"


def _valid_payload() -> dict:
    return {
        "summary": "One finding.",
        "comments": [
            {
                "path": "src/main.py",
                "line": 42,
                "severity": "important",
                "type": "bug",
                "body": "**Diverging state.** The row and rank disagree while browsing.",
            }
        ],
    }


def _forward(stderr_text: str, tmp_path: Path) -> str:
    """Run log_degradation_warnings on a captured-stderr file, return its log
    output (log_info writes to stderr)."""
    stderr_file = tmp_path / "extract-err.txt"
    stderr_file.write_text(stderr_text)
    result = subprocess.run(
        ["bash", "-c", f"source {LIB}; log_degradation_warnings {stderr_file}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stderr


def test_malformed_lens_payload_reaches_daemon_log(tmp_path):
    # The acceptance case: one lens's payload is malformed, the merge still
    # succeeds on the other lens, and the merge-skip warning lands in the log.
    good = tmp_path / ".pr-review-raw.txt"
    good.write_text(_wrap(_valid_payload()))
    bad = tmp_path / ".pr-review-raw-perf.txt"
    bad.write_text("no json fence here at all")
    merge = subprocess.run(
        ["python3", str(MERGE), "--no-style", str(good), str(bad)],
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    assert "merge-skip: perf" in merge.stderr
    log = _forward(merge.stderr, tmp_path)
    assert "[pr-review-agent]" in log
    assert "merge-skip: perf payload failed" in log


def test_forwards_all_three_warning_kinds(tmp_path):
    log = _forward(
        "merge-skip: perf payload failed (no-json-fence): x\n"
        "finding-skip: comments[2] failed validation: y\n"
        "confidence-gate: dropped 3 finding(s) below 80\n",
        tmp_path,
    )
    assert "merge-skip: perf payload failed" in log
    assert "finding-skip: comments[2] failed validation" in log
    assert "confidence-gate: dropped 3 finding(s)" in log


def test_unrelated_stderr_noise_is_not_forwarded(tmp_path):
    # Truncation counts get their own surfacing in the posted summary; python
    # tracebacks belong to the failure branch. Neither should be re-logged.
    log = _forward(
        "truncated_count=4\nTraceback (most recent call last):\n",
        tmp_path,
    )
    assert log == ""


def test_missing_stderr_file_is_a_noop(tmp_path):
    result = subprocess.run(
        ["bash", "-c", f"source {LIB}; log_degradation_warnings {tmp_path}/absent.txt"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stderr == ""
