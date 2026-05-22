"""Snapshot test for daemon/post-review.sh's --dry-run payload.

Fixture is `tests/fixtures/post_review_snapshot/` and holds:
- anchored.json: 2 findings (single-line `important+bug`, range `nit+refactor`)
- unanchored.json: 1 finding (`pre_existing+polish`) routed to ## Additional findings
- summary.txt: review summary
- expected_payload.json: the exact gh api payload the daemon should emit

Regenerate the snapshot (e.g. after intentionally changing the format) by:

    bash daemon/post-review.sh --owner taewanu --repo pr-review-agent --number 999 \\
        --summary-file tests/fixtures/post_review_snapshot/summary.txt \\
        --anchored tests/fixtures/post_review_snapshot/anchored.json \\
        --unanchored tests/fixtures/post_review_snapshot/unanchored.json \\
        --head-sha abc123def456 --dry-run \\
        > tests/fixtures/post_review_snapshot/expected_payload.json
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "post_review_snapshot"
DAEMON = REPO_ROOT / "daemon"


def test_dry_run_payload_matches_snapshot():
    result = subprocess.run(
        [
            "bash",
            str(DAEMON / "post-review.sh"),
            "--owner",
            "taewanu",
            "--repo",
            "pr-review-agent",
            "--number",
            "999",
            "--summary-file",
            str(FIXTURE / "summary.txt"),
            "--anchored",
            str(FIXTURE / "anchored.json"),
            "--unanchored",
            str(FIXTURE / "unanchored.json"),
            "--head-sha",
            "abc123def456",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    actual = json.loads(result.stdout)
    expected = json.loads((FIXTURE / "expected_payload.json").read_text())
    assert actual == expected, (
        "post-review.sh --dry-run payload drifted from snapshot. "
        "Regenerate per the docstring if the change was intentional."
    )
