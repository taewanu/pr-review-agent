"""Snapshot tests for daemon/post-review.sh's --dry-run payload.

Fixture is `tests/fixtures/post_review_snapshot/` and holds:
- anchored.json: 2 findings (single-line `important+bug`, range `nit+refactor`)
- unanchored.json: 1 finding (`pre_existing+polish`) routed to ## Additional findings
- summary.txt: review summary
- expected_payload.json: payload when no findings were dropped (default path)
- expected_payload_dropped_2.json: payload when 2 forbidden-combo findings were dropped

Regenerate the default snapshot (canonical identity pinned via env so the
snapshot stays stable regardless of which fork's checkout runs the tests):

    PR_REVIEW_PROJECT_URL=https://github.com/taewanu/pr-review-agent \\
    PR_REVIEW_PROJECT_NAME=pr-review-agent \\
    bash daemon/post-review.sh --owner taewanu --repo pr-review-agent --number 999 \\
        --summary-file tests/fixtures/post_review_snapshot/summary.txt \\
        --anchored tests/fixtures/post_review_snapshot/anchored.json \\
        --unanchored tests/fixtures/post_review_snapshot/unanchored.json \\
        --head-sha abc123def456 --dry-run \\
        > tests/fixtures/post_review_snapshot/expected_payload.json

Regenerate the dropped-combo snapshot by appending `--dropped-combo 2` and
redirecting to `expected_payload_dropped_2.json`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "post_review_snapshot"
DAEMON = REPO_ROOT / "daemon"
# Strip these from inherited env so each test starts from a known baseline.
# Tests that need a specific identity pass `env=` explicitly.
_OVERRIDE_KEYS = ("PR_REVIEW_PROJECT_URL", "PR_REVIEW_PROJECT_NAME")
CANONICAL_URL = "https://github.com/taewanu/pr-review-agent"
CANONICAL_NAME = "pr-review-agent"
CANONICAL_ENV = {
    "PR_REVIEW_PROJECT_URL": CANONICAL_URL,
    "PR_REVIEW_PROJECT_NAME": CANONICAL_NAME,
}


def _run_post_review(*extra_args: str, env: dict | None = None, check: bool = True):
    base_env = {k: v for k, v in os.environ.items() if k not in _OVERRIDE_KEYS}
    return subprocess.run(
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
        check=check,
        env={**base_env, **(env or {})},
    )


def _payload(*extra_args: str, env: dict | None = None) -> dict:
    return json.loads(_run_post_review(*extra_args, env=env).stdout)


def _git_remote_identity() -> tuple[str, str]:
    """Owner and repo derived from the local git origin. Used by the derive
    test to stay correct on both canonical and fork checkouts."""
    url = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)(?:\.git)?$", url)
    assert match, f"unexpected git remote URL format: {url}"
    return match.group(1), match.group(2)


def test_dry_run_payload_matches_snapshot():
    # Pin canonical identity via env so the snapshot stays stable regardless of
    # which fork's checkout is running the tests.
    actual = _payload(env=CANONICAL_ENV)
    expected = json.loads((FIXTURE / "expected_payload.json").read_text())
    assert actual == expected, (
        "post-review.sh --dry-run payload drifted from snapshot. "
        "Regenerate per the docstring if the change was intentional."
    )


def test_dry_run_payload_with_dropped_combo_matches_snapshot():
    # Locks in the ADR 0005 per-finding-failure rendering: an italic note sits
    # between the summary and `## Additional findings` so the operator sees the
    # redaction in the body itself, not just in stderr.
    actual = _payload("--dropped-combo", "2", env=CANONICAL_ENV)
    expected = json.loads((FIXTURE / "expected_payload_dropped_2.json").read_text())
    assert actual == expected, (
        "post-review.sh --dry-run --dropped-combo 2 payload drifted from snapshot. "
        "Regenerate per the docstring if the change was intentional."
    )


def test_derives_project_identity_from_git_remote_when_env_unset():
    # Zero-config path: with neither env var set, the daemon parses the local
    # git origin to fill the footer/banner. Works for canonical and any fork.
    owner, repo = _git_remote_identity()
    body = _payload()["body"]
    assert f"[{repo}](https://github.com/{owner}/{repo})" in body


def test_env_vars_override_git_remote_derivation():
    # The core forking guarantee: when an operator explicitly sets both env
    # vars (e.g. their fork's identity differs from the cloned repo), neither
    # the canonical owner nor the canonical project name appears in the body.
    fork_url = "https://github.com/myfork/my-review-tool"
    fork_name = "my-review-tool"
    body = _payload(
        env={
            "PR_REVIEW_PROJECT_URL": fork_url,
            "PR_REVIEW_PROJECT_NAME": fork_name,
        }
    )["body"]
    assert f"[{fork_name}]({fork_url})" in body
    assert "taewanu" not in body, "upstream owner must not leak when explicitly overridden"
    assert "pr-review-agent" not in body, (
        "canonical project name must not leak when explicitly overridden"
    )
