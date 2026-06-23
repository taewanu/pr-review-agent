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


def test_render_carries_provenance_tag():
    # The Status comment is agent-authored, so it carries the Provenance tag like
    # every posted artifact that is not a Review body (ADR 0010).
    out, rc, _ = _run("render_status_comment 'h' 'full PR' 1 'only.sh'")
    assert rc == 0
    assert "🤖 _pr-review-agent_" in out


def test_render_is_scope_only_never_findings():
    # The status comment must never carry the review body/findings — that would
    # duplicate the Review object (#60). render takes only scope inputs, so a
    # findings string handed in as the file list is the closest a caller could
    # come; assert the structure stays scope-shaped (no severity/type emoji that
    # only the review body carries).
    out, _, _ = _run(
        "render_status_comment '✅ Reviewed `abc123`: 3 findings' 'full PR' 1 'daemon/poll.sh'"
    )
    assert "🔴" not in out and "🐛" not in out
    assert "AI-drafted" not in out


# --- status_sha_link / status_scope_link (head-line + scope builders) -----
# These pure helpers (lib.sh) build the linked head SHA and linked scope range
# that review-pr.sh passes into render_status_comment (#102). The head line uses
# ": N findings", never " — N findings", and SHAs link to the HEAD repo.

_REPO = "https://github.com/example/example"
_HEAD = "abcdef0123456789abcdef0123456789abcdef01"
_LAST = "0123456789abcdef0123456789abcdef01234567"


def test_status_sha_link_is_commit_markdown_link():
    out, rc, _ = _run(f"status_sha_link '{_REPO}' '{_HEAD}'")
    assert rc == 0
    # Display = 12-char short SHA, backtick-wrapped; href = full SHA on /commit/.
    assert out.strip() == f"[`{_HEAD[:12]}`]({_REPO}/commit/{_HEAD})"


def test_status_scope_link_real_range_is_compare_link():
    out, rc, _ = _run(f"status_scope_link '{_REPO}' '{_LAST}' '{_HEAD}'")
    assert rc == 0
    # Display = short..short; href = /compare/<full>...<full> (THREE dots).
    assert out.strip() == f"[`{_LAST[:12]}..{_HEAD[:12]}`]({_REPO}/compare/{_LAST}...{_HEAD})"
    assert "..." in out  # three-dot compare ref, not the two-dot display range


def test_status_scope_link_full_pr_is_unlinked():
    out, rc, _ = _run(f"status_scope_link '{_REPO}' '' '{_HEAD}'")
    assert rc == 0
    assert out.strip() == "full PR"
    assert "](" not in out  # no markdown link


def test_reviewed_head_line_uses_colon_not_em_dash():
    # The reviewed head line review-pr.sh builds: linked SHA, then ": N findings"
    # (the em dash is the one required visible-prose fix, #102).
    out, rc, _ = _run(
        f"printf \"✅ Reviewed %s: 3 findings\" \"$(status_sha_link '{_REPO}' '{_HEAD}')\""
    )
    assert rc == 0
    assert "—" not in out
    assert ": 3 findings" in out
    assert f"[`{_HEAD[:12]}`]({_REPO}/commit/{_HEAD})" in out


def test_render_singular_file_noun():
    out, _, _ = _run("render_status_comment 'h' 'full PR' 1 'only.sh'")
    assert "<summary>1 file</summary>" in out


def test_render_inserts_body_block_between_scope_and_files():
    # The failed status comment (#180) and the findings index both ride the 5th
    # arg, dropped in as its own paragraph between the scope line and the file list.
    out, rc, _ = _run(
        "render_status_comment 'h' 'full PR' 1 'only.sh' '> The review agent timed out.'"
    )
    assert rc == 0
    assert "> The review agent timed out." in out
    assert out.index("_Scope: full PR_") < out.index("> The review agent timed out.")
    assert out.index("> The review agent timed out.") < out.index("<summary>1 file</summary>")


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


# --- status_failure_reason (#180) -----------------------------------------
# Maps a log_failure category to the author-facing reason on the failed status
# head-line. Surfaces a phrase only where it helps the author; internal hiccups
# return empty so the caller drops the reason line and the bare "will retry next
# cycle" head-line carries the message.


def test_status_failure_reason_timeouts_share_one_phrase():
    # review and editor stages both read as "the review" to the author.
    for category in ("review-timeout", "edit-timeout"):
        out, rc, _ = _run(f"status_failure_reason {category}")
        assert rc == 0
        assert out == "The review agent timed out."


def test_status_failure_reason_pending_conflict_is_surfaced():
    out, rc, _ = _run("status_failure_reason pending-conflict")
    assert rc == 0
    assert out == "An earlier review is still pending on this PR."


def test_status_failure_reason_internal_hiccups_are_silent():
    # Categories the author can't act on return empty, so the caller drops the
    # reason line rather than print an internal slug.
    for category in (
        "empty-stdout",
        "no-fence",
        "parse-error",
        "schema-invalid",
        "style-violation",
        "edit-empty",
        "post-failed",
        "unknown",
    ):
        out, rc, _ = _run(f"status_failure_reason {category}")
        assert rc == 0
        assert out == ""


def test_status_failure_reason_phrases_are_em_dash_free():
    # The failed head-line is fixed chrome that skips the voice.py gate, so the
    # no-em-dash rule is enforced on these phrases directly.
    for category in ("review-timeout", "pending-conflict"):
        out, _, _ = _run(f"status_failure_reason {category}")
        assert "—" not in out


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
