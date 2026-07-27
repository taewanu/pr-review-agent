"""Tests for daemon/lib.sh's `derive_pr_metadata`, the one PR metadata fetch.

review-pr.sh and reply-pr.sh both read PR metadata off this function's stdout, so
what it asks gh for and when it refuses are the whole contract. Tests stub `gh`
via a tmpdir prepended to `PATH`, exercising the real guard without a network or
an App key.

The snippets run under `set -euo pipefail`: both entry points source lib.sh with
those flags, and the callers invoke this in `meta="$(derive_pr_metadata …)" ||
exit 1`, which suspends `errexit` inside the function. A guard that leaned on
`set -e` to abort would pass in a bare shell and fall through in the daemon.
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

PR_URL = "https://github.com/example/example/pull/1"
HEAD_SHA = "a" * 40


def _complete_meta(**overrides) -> dict:
    meta = {
        "id": "PR_kwabc",
        "headRepository": {"name": "example"},
        "headRepositoryOwner": {"login": "contributor"},
        "headRefName": "feature-branch",
        "headRefOid": HEAD_SHA,
        "baseRefName": "main",
        "title": "a title",
        "body": "a body",
        "closingIssuesReferences": [],
        "commits": [],
    }
    meta.update(overrides)
    return meta


def _run(
    payload: dict | str | None,
    *,
    fail: bool = False,
    fail_stderr: str = "",
) -> tuple[str, int, str, str]:
    """Invoke derive_pr_metadata against a stub `gh` that records its argv.

    Returns (stdout, exit code, stderr, the argv line the stub saw). Pass
    `fail=True` to make gh exit non-zero, or a raw string payload for a wire
    shape json.dumps cannot produce.
    """
    with tempfile.TemporaryDirectory() as tmp:
        argv_log = Path(tmp) / "argv"
        if fail:
            body = f"printf '%s\\n' {json.dumps(fail_stderr)} >&2\nexit 1"
        else:
            text = payload if isinstance(payload, str) else json.dumps(payload)
            body = "cat <<'JSON_EOF'\n" + text + "\nJSON_EOF"
        stub = Path(tmp) / "gh"
        # Heredoc terminators must sit at column 0, so the body is not indented.
        stub.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >{argv_log}\n{body}\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = os.environ.copy()
        env["PATH"] = f"{tmp}:{env['PATH']}"
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"set -euo pipefail; source {LIB}; source {APP_STUB}; "
                f'derive_pr_metadata "{PR_URL}"',
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        argv = argv_log.read_text().strip() if argv_log.exists() else ""
        return result.stdout, result.returncode, result.stderr, argv


def test_prints_the_metadata_gh_returned():
    meta = _complete_meta()
    stdout, rc, _, _ = _run(meta)
    assert rc == 0
    assert json.loads(stdout) == meta


def test_requests_every_field_its_callers_read():
    # The point of the seam: one --json list, so a field one entry point starts
    # reading cannot be missing for the other. These are the fields review-pr.sh
    # and reply-pr.sh read off the blob.
    _, rc, _, argv = _run(_complete_meta())
    assert rc == 0
    requested = argv.split("--json ")[1].split(" ")[0].split(",")
    for field in (
        "id",
        "headRepository",
        "headRepositoryOwner",
        "headRefName",
        "headRefOid",
        "baseRefName",
        "title",
        "body",
        "closingIssuesReferences",
        "commits",
    ):
        assert field in requested


def test_refuses_when_a_head_field_is_missing():
    # A closed PR whose fork was deleted: gh succeeds, the head fields are empty,
    # and every clone and anchor step downstream would work off blanks.
    for field in ("headRepository", "headRepositoryOwner", "headRefName", "headRefOid"):
        stdout, rc, stderr, _ = _run(_complete_meta(**{field: None}))
        assert rc == 1, field
        assert stdout == "", field
        assert "incomplete metadata" in stderr, field


def test_refuses_when_a_head_field_is_empty():
    stdout, rc, stderr, _ = _run(_complete_meta(headRefOid=""))
    assert rc == 1
    assert stdout == ""
    assert "incomplete metadata" in stderr


def test_refuses_when_gh_fails():
    stdout, rc, stderr, _ = _run(None, fail=True, fail_stderr="HTTP 404: Not Found")
    assert rc == 1
    assert stdout == ""
    assert "gh pr view failed" in stderr
    # gh's own stderr passes through, so an operator can tell a 404 from a
    # rate-limit rather than reading one opaque failure line.
    assert "HTTP 404: Not Found" in stderr


def test_refuses_when_the_response_is_not_json():
    stdout, rc, stderr, _ = _run("not json at all")
    assert rc == 1
    assert stdout == ""
    assert "incomplete metadata" in stderr


def test_callers_do_not_fetch_pr_metadata_themselves():
    # The duplication this seam replaced was two `gh pr view` calls drifting
    # apart; a caller that grows its own again puts it right back.
    for script in ("review-pr.sh", "reply-pr.sh"):
        text = (REPO_ROOT / "daemon" / script).read_text()
        assert "gh pr view" not in text, script
        assert "derive_pr_metadata" in text, script
