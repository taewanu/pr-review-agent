"""Snapshot tests for daemon/post-review.sh's --dry-run payload.

Fixture is `tests/fixtures/post_review_snapshot/` and holds:
- anchored.json: 2 findings (single-line `important+bug`, range `nit+refactor`)
- unanchored.json: 1 finding (`pre_existing+polish`) routed to ## Additional findings
- summary.txt: review summary
- expected_payload.json: payload when no findings were dropped (default path)
- expected_payload_dropped_2.json: payload when 2 forbidden-combo findings were dropped

Regenerate the default snapshot:

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
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "post_review_snapshot"
DAEMON = REPO_ROOT / "daemon"
# Scrub these from the inherited env so each test starts from a known state;
# the daemon now refuses to run without them set, so tests must pass them in
# explicitly via `env=` to exercise the canonical or fork rendering path.
_OVERRIDE_KEYS = ("PR_REVIEW_PROJECT_URL", "PR_REVIEW_PROJECT_NAME")
CANONICAL_URL = "https://github.com/taewanu/pr-review-agent"
CANONICAL_NAME = "pr-review-agent"
CANONICAL_ENV = {
    "PR_REVIEW_PROJECT_URL": CANONICAL_URL,
    "PR_REVIEW_PROJECT_NAME": CANONICAL_NAME,
}


def _run_post_review(*extra_args: str, env: dict | None = None, check: bool = True):
    base_env = {k: v for k, v in os.environ.items() if k not in _OVERRIDE_KEYS}
    # Disable .env loading by default so a developer's local .env can't
    # contaminate test results. Specific tests can re-enable by passing
    # `env={"PR_REVIEW_ENV_FILE": "<path>", ...}`.
    base_env["PR_REVIEW_ENV_FILE"] = "/dev/null"
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


def test_dry_run_payload_matches_snapshot():
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


def test_no_canonical_leak_when_fully_overridden():
    # The core forking guarantee: when an operator sets both env vars to their
    # own values, neither the upstream owner ("taewanu") nor the canonical
    # project name ("pr-review-agent") appears anywhere in the posted body.
    fork_url = "https://github.com/myfork/my-review-tool"
    fork_name = "my-review-tool"
    body = _payload(
        env={
            "PR_REVIEW_PROJECT_URL": fork_url,
            "PR_REVIEW_PROJECT_NAME": fork_name,
        }
    )["body"]
    assert f"[{fork_name}]({fork_url})" in body
    assert "taewanu" not in body, "upstream owner must not leak into forks' output"
    assert "pr-review-agent" not in body, "canonical project name must not leak into forks' output"


@pytest.mark.parametrize(
    "env,missing_var",
    [
        ({"PR_REVIEW_PROJECT_NAME": CANONICAL_NAME}, "PR_REVIEW_PROJECT_URL"),
        ({"PR_REVIEW_PROJECT_URL": CANONICAL_URL}, "PR_REVIEW_PROJECT_NAME"),
        ({}, "PR_REVIEW_PROJECT_URL"),
    ],
)
def test_missing_required_env_var_exits_non_zero(env, missing_var):
    # No silent fallback to canonical: daemon must refuse partial or missing
    # config and name the offending variable so the operator can fix it.
    result = _run_post_review(env=env, check=False)
    assert result.returncode != 0
    assert missing_var in result.stderr
    assert "required" in result.stderr


def test_env_file_supplies_project_identity(tmp_path):
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "PR_REVIEW_PROJECT_URL=https://github.com/fromfile/repo\n"
        "PR_REVIEW_PROJECT_NAME=fromfile-name\n"
    )
    body = _payload(env={"PR_REVIEW_ENV_FILE": str(env_file)})["body"]
    assert "[fromfile-name](https://github.com/fromfile/repo)" in body


def test_shell_env_wins_over_env_file(tmp_path):
    # `.env` is the persistent source of truth, but an inline `VAR=…` invocation
    # must override it for one-off testing (option (a) precedence).
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "PR_REVIEW_PROJECT_URL=https://github.com/fromfile/repo\n"
        "PR_REVIEW_PROJECT_NAME=fromfile-name\n"
    )
    body = _payload(
        env={
            "PR_REVIEW_ENV_FILE": str(env_file),
            "PR_REVIEW_PROJECT_URL": "https://github.com/fromshell/repo",
            "PR_REVIEW_PROJECT_NAME": "fromshell-name",
        }
    )["body"]
    assert "[fromshell-name](https://github.com/fromshell/repo)" in body
    assert "fromfile" not in body, "shell env must win over .env file"
