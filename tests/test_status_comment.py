"""Tests for the edit-in-place status-comment helpers in daemon/lib.sh (#60).

One durable status comment per PR: posted at review start (`👀 Reviewing`),
edited in place when the review lands (`✅ Reviewed … N findings`), and reused
across ticks rather than re-posted. `render_status_comment` builds the body
(scope only — never the review findings), `diff_paths` derives the file list
from the diff, and `find_status_comment` / `post_status_comment` /
`edit_status_comment` are the gh-backed post/find/edit operations — all
best-effort so a flaky status comment never aborts a review. `gh` is stubbed via
a tmpdir on PATH, mirroring test_sentinel_discovery; the stub records its argv so
a test can assert which endpoint was hit.
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


# --- render_status_comment / diff_paths (no gh) ---------------------------


def test_render_carries_header_scope_and_marker():
    out, rc, _ = _run(
        "render_status_comment '👀 Reviewing `abc123`…' 'full PR' 2 "
        "\"$(printf 'daemon/poll.sh\\nREADME.md')\""
    )
    assert rc == 0
    assert "👀 Reviewing `abc123`…" in out
    assert "_Scope: full PR_" in out
    assert "<summary>2 files</summary>" in out
    assert "- `daemon/poll.sh`" in out
    assert "- `README.md`" in out
    # The marker find_status_comment keys on must be present.
    assert "<!-- pr-review-agent:status -->" in out


def test_render_is_scope_only_never_findings():
    # The status comment must never carry the review body/findings — that would
    # duplicate the Review object (#60). render takes only scope inputs, so a
    # findings string handed in as the file list is the closest a caller could
    # come; assert the structure stays scope-shaped (no severity/type emoji that
    # only the review body carries).
    out, _, _ = _run(
        "render_status_comment '✅ Reviewed `abc123` — 3 findings' 'full PR' 1 'daemon/poll.sh'"
    )
    assert "🔴" not in out and "🐛" not in out
    assert "AI-drafted" not in out


def test_render_singular_file_noun():
    out, _, _ = _run("render_status_comment 'h' 'full PR' 1 'only.sh'")
    assert "<summary>1 file</summary>" in out


def test_diff_paths_extracts_post_image_paths():
    with tempfile.TemporaryDirectory() as tmp:
        diff = Path(tmp) / "d.txt"
        diff.write_text(
            "diff --git a/daemon/poll.sh b/daemon/poll.sh\n"
            "index 111..222 100644\n"
            "--- a/daemon/poll.sh\n"
            "+++ b/daemon/poll.sh\n"
            "@@ -1 +1 @@\n"
            "-old\n+new\n"
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n+++ b/README.md\n"
        )
        out, rc, _ = _run(f"diff_paths {diff}")
        assert rc == 0
        assert out.splitlines() == ["daemon/poll.sh", "README.md"]


def test_diff_paths_empty_on_missing_file():
    out, rc, _ = _run("diff_paths /nonexistent/diff.txt")
    assert rc == 0
    assert out.strip() == ""


# --- find_status_comment --------------------------------------------------


def test_find_status_comment_prints_last_matching_id():
    # The stub echoes gh_stdout verbatim (it does not run --jq); the function's
    # `tail -1` picks the last id when more than one slipped through.
    out, rc, calls = _run(
        "find_status_comment owner repo 7 octocat",
        gh_stdout="111\n222",
    )
    assert rc == 0
    assert out.strip() == "222"
    assert "issues/7/comments" in calls
    assert "--paginate" in calls


def test_find_status_comment_empty_operator_skips_gh():
    out, rc, calls = _run('find_status_comment owner repo 7 ""')
    assert rc == 0
    assert out.strip() == ""
    assert calls.strip() == ""


def test_find_status_comment_swallows_failure():
    out, rc, _ = _run("find_status_comment owner repo 7 octocat", gh_exit=1)
    assert rc == 0
    assert out.strip() == ""


# --- post_status_comment --------------------------------------------------


def test_post_status_comment_prints_id():
    out, rc, calls = _run(
        "post_status_comment owner repo 7 'body'",
        gh_stdout="12345",
    )
    assert rc == 0
    assert out.strip() == "12345"
    assert "issues/7/comments" in calls


def test_post_status_comment_swallows_failure():
    out, rc, _ = _run("post_status_comment owner repo 7 'body'", gh_exit=1)
    assert rc == 0
    assert out.strip() == ""


# --- edit_status_comment --------------------------------------------------


def test_edit_status_comment_patches_the_id():
    _, rc, calls = _run("edit_status_comment owner repo 999 'new body'")
    assert rc == 0
    assert "PATCH" in calls
    assert "issues/comments/999" in calls


def test_edit_status_comment_noop_on_empty_id():
    _, rc, calls = _run('edit_status_comment owner repo "" "new body"')
    assert rc == 0
    assert calls.strip() == ""


def test_edit_status_comment_swallows_failure():
    _, rc, _ = _run("edit_status_comment owner repo 999 'new body'", gh_exit=1)
    assert rc == 0
