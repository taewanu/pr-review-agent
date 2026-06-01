"""Tests for the pickup-ack helpers in daemon/lib.sh (#48).

`post_pickup_ack` posts a transient "reviewing" PR comment and prints the new
comment id; `delete_comment` removes it once the review lands. Both are
best-effort — they must return 0 even when `gh` fails, so a flaky ack never
aborts a review. `gh` is stubbed via a tmpdir on PATH, mirroring
test_sentinel_discovery; the stub records its argv so a test can assert which
endpoint was hit.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"


def _run(call: str, *, gh_stdout: str = "", gh_exit: int = 0) -> tuple[str, int, str]:
    """Source lib.sh and run `call` with a stubbed `gh`. Returns
    (stdout, returncode, recorded gh argv)."""
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "gh_calls.log"
        stub = Path(tmp) / "gh"
        out = gh_stdout.replace("'", "'\\''")
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{log}"\n'
            f"printf '%s' '{out}'\n"
            f"exit {gh_exit}\n"
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = os.environ.copy()
        env["PATH"] = f"{tmp}:{env['PATH']}"
        result = subprocess.run(
            ["bash", "-c", f"source {LIB}; {call}"],
            capture_output=True,
            text=True,
            env=env,
        )
        calls = log.read_text() if log.exists() else ""
        return result.stdout, result.returncode, calls


def test_post_pickup_ack_prints_comment_id():
    out, rc, calls = _run(
        "post_pickup_ack owner repo 7 abcdef1234567890",
        gh_stdout="12345",
    )
    assert rc == 0
    assert out.strip() == "12345"
    # Posted to the PR's issue-comments endpoint.
    assert "issues/7/comments" in calls


def test_post_pickup_ack_swallows_failure():
    # A failed ack must not surface an id or a non-zero rc — the caller treats
    # empty stdout as "no ack" and carries on with the review.
    out, rc, _ = _run("post_pickup_ack owner repo 7 abcdef1234567890", gh_exit=1)
    assert rc == 0
    assert out.strip() == ""


def test_delete_comment_deletes_the_id():
    _, rc, calls = _run("delete_comment owner repo 999")
    assert rc == 0
    assert "DELETE" in calls
    assert "issues/comments/999" in calls


def test_delete_comment_noop_on_empty_id():
    # cleanup() passes an empty id when no ack was posted — gh must not be called.
    _, rc, calls = _run('delete_comment owner repo ""')
    assert rc == 0
    assert calls.strip() == ""


def test_delete_comment_swallows_failure():
    _, rc, _ = _run("delete_comment owner repo 999", gh_exit=1)
    assert rc == 0
