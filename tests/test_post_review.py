"""Snapshot tests for daemon/post-review.sh's --dry-run payload.

Fixture is `tests/fixtures/post_review_snapshot/` and holds:
- anchored.json: 2 findings (single-line `important+bug`, range `nit+refactor`)
- unanchored.json: 1 finding (`pre_existing+polish`) routed to ## Additional findings
- summary.txt: review summary
- expected_payload.json: payload when no findings were dropped (default path)
- expected_payload_dropped_2.json: payload when 2 forbidden-combo findings were dropped

Snapshots are pinned to whatever identity the daemon derives from
`git remote get-url origin` of the checkout where they were generated. The
checked-in fixtures match the canonical (`taewanu/pr-review-agent`) clone.
A fork that runs these tests will see derive return the fork's identity,
snapshots will fail, and the fork should regenerate them from their checkout:

    bash daemon/post-review.sh --owner <owner> --repo <repo> --number 0 \\
        --summary-file tests/fixtures/post_review_snapshot/summary.txt \\
        --anchored tests/fixtures/post_review_snapshot/anchored.json \\
        --unanchored tests/fixtures/post_review_snapshot/unanchored.json \\
        --head-sha abc123def456 --dry-run \\
        > tests/fixtures/post_review_snapshot/expected_payload.json

`--owner`/`--repo`/`--number` are required CLI flags but feed only the API
endpoint URL, which `--dry-run` skips — they do not affect the payload, so
any values work for regen.

Regenerate the dropped-combo snapshot by appending `--dropped-combo 2` and
redirecting to `expected_payload_dropped_2.json`.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "post_review_snapshot"
DAEMON = REPO_ROOT / "daemon"


def _run_post_review(*extra_args: str) -> dict:
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
            *extra_args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _git_remote_identity() -> tuple[str, str]:
    """Owner and repo derived from the local git origin — same parse the
    daemon does, used by the derive test to stay correct on any checkout."""
    url = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?$", url)
    assert match, f"unexpected git remote URL format: {url}"
    return match.group(1), match.group(2)


def test_dry_run_payload_matches_snapshot():
    actual = _run_post_review()
    expected = json.loads((FIXTURE / "expected_payload.json").read_text())
    assert actual == expected, (
        "post-review.sh --dry-run payload drifted from snapshot. "
        "Regenerate per the docstring if the change was intentional."
    )


def test_dry_run_payload_with_dropped_combo_matches_snapshot():
    # Locks in the ADR 0005 per-finding-failure rendering: an italic note sits
    # between the summary and `## Additional findings` so the operator sees the
    # redaction in the body itself, not just in stderr.
    actual = _run_post_review("--dropped-combo", "2")
    expected = json.loads((FIXTURE / "expected_payload_dropped_2.json").read_text())
    assert actual == expected, (
        "post-review.sh --dry-run --dropped-combo 2 payload drifted from snapshot. "
        "Regenerate per the docstring if the change was intentional."
    )


def test_footer_reflects_git_remote_identity():
    # Zero-config path: the daemon parses the local git origin to fill the
    # footer/banner. Body must surface that derived identity. Test stays
    # correct on canonical and fork checkouts alike by querying git directly.
    owner, repo = _git_remote_identity()
    body = _run_post_review()["body"]
    assert f"[{repo}](https://github.com/{owner}/{repo})" in body
