"""Tests for reply-pr.sh's reply-scoped per-PR lock (#198).

The review path serializes overlapping runs via acquire_pr_lock (#67), but the
reply path had no lock: a manual reply-pr.sh overlapping a daemon tick would
both read the PR's comments before either's ack posted, and both dispatch the
same thread. These tests drive the real script with a stub `gh`, so the lock
acquire/skip/release behavior is what reply-pr.sh actually does, not a replica.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

from app_auth_fixture import install_app_stubs

REPO_ROOT = Path(__file__).resolve().parent.parent
REPLY_SH = REPO_ROOT / "daemon" / "reply-pr.sh"

PR_URL = "https://github.com/example/example/pull/7"
REPLY_LOCK_NAME = "example-example-7-reply.lock"


def _stub_bin(tmp_path: Path) -> Path:
    """A PATH dir whose `gh` answers the preflight and returns zero comments,
    recording a marker file when the comments endpoint is hit. `claude` exists
    but is never reached on the no-replies path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '*"/pulls/"*"/comments"*)\n'
        f"touch {tmp_path}/comments-api-hit\n"
        'echo "[]" ;;\n'
        "*) exit 0 ;;\n"
        "esac\n"
    )
    claude = bin_dir / "claude"
    claude.write_text("#!/usr/bin/env bash\nexit 0\n")
    for stub in (gh, claude):
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    install_app_stubs(bin_dir)
    return bin_dir


def _run(tmp_path: Path) -> subprocess.CompletedProcess:
    bin_dir = _stub_bin(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["APP_KEY_PATH"] = str(bin_dir / "app.pem")
    env["PR_REVIEW_STATE_DIR"] = str(tmp_path / "state")
    return subprocess.run(
        ["bash", str(REPLY_SH), "--app-id", "4361858", PR_URL],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_no_replies_run_takes_and_releases_the_lock(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "no unaddressed replies" in result.stderr
    # Released on exit: no lock left behind for the next tick to skip on.
    assert not (tmp_path / "state" / REPLY_LOCK_NAME).exists()


def test_live_lock_skips_before_reading_comments(tmp_path):
    # A live holder (this test process) has the reply pass in flight; the
    # overlapping run must exit 0 without ever listing the PR's comments —
    # reading first is exactly the double-dispatch window.
    state = tmp_path / "state"
    state.mkdir()
    (state / REPLY_LOCK_NAME).write_text(f"{os.getpid()} {int(time.time())}\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "already in progress" in result.stderr
    assert not (tmp_path / "comments-api-hit").exists()
    # The held lock belongs to the other run: skipping must not release it.
    assert (state / REPLY_LOCK_NAME).exists()


def test_reply_lock_does_not_contend_with_review_lock(tmp_path):
    # The reply-scoped key must stay disjoint from the review path's (#67)
    # lock: a running review of the same PR never blocks its reply pass.
    state = tmp_path / "state"
    state.mkdir()
    (state / "example-example-7.lock").write_text(f"{os.getpid()} {int(time.time())}\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "no unaddressed replies" in result.stderr
