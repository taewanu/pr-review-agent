"""Tests for daemon/lib.sh's `discover_sentinel_sha`.

The function shells out to `gh api` to read PR reviews and greps each body for
the ADR 0006 sentinel. Tests stub `gh` via a tmpdir prepended to `PATH` so the
function exercises its real jq pipeline without touching the network.

Exit code contract is load-bearing — the daemon caller uses it to distinguish
API failure (state fallback only, never first-review) from API-success-but-no-
sentinel (state fallback, first-review allowed on empty state).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"

SENTINEL_SHA_A = "a" * 40
SENTINEL_SHA_B = "b" * 40
SENTINEL_SHA_C = "c" * 40


def _review(
    body: str,
    *,
    login: str = "operator",
    submitted_at: str | None = "2026-05-28T10:00:00Z",
    created_at: str = "2026-05-28T09:55:00Z",
) -> dict:
    return {
        "user": {"login": login},
        "body": body,
        "submitted_at": submitted_at,
        "created_at": created_at,
    }


def _comment(
    body: str,
    *,
    login: str = "operator",
    created_at: str = "2026-05-28T10:00:00Z",
    updated_at: str | None = None,
) -> dict:
    # updated_at defaults to created_at (a never-edited comment): the status
    # comment (ADR 0024) is the exception, created once and edited every tick.
    return {
        "user": {"login": login},
        "body": body,
        "created_at": created_at,
        "updated_at": updated_at if updated_at is not None else created_at,
    }


def _sentinel_body(sha: str) -> str:
    return f"summary\n\n---\n\n_AI-drafted_\n<!-- pr-review-agent:sha:{sha} -->"


def _run(
    reviews: list[dict] | str,
    comments: list[dict] | None = None,
    login: str = "operator",
    fail_stderr: str = "",
    fail_comments: bool = False,
) -> tuple[str, int, str]:
    """Invoke discover_sentinel_sha with a stub `gh` that dispatches by URL.

    discover_sentinel_sha makes two `gh api` calls — `.../pulls/N/reviews`
    (primary) and `.../issues/N/comments` (the #49 body-wipe backup). The stub
    matches on the requested path so each call gets its own payload.

    Pass `reviews="FAIL"` to make the reviews call exit non-zero (the function
    must then return 2 before it ever reaches comments). `fail_comments=True`
    fails only the comments call, exercising the degrade-to-reviews-only path.
    `fail_stderr` is the message a failing call writes to stderr.
    """
    import tempfile

    def _branch(data: list[dict], fail: bool) -> str:
        if fail:
            line = fail_stderr.replace("'", "'\\''")
            return f"echo '{line}' >&2\nexit 1"
        return "cat <<'JSON_EOF'\n" + json.dumps(data) + "\nJSON_EOF"

    reviews_fail = reviews == "FAIL"
    reviews_branch = _branch([] if reviews_fail else reviews, reviews_fail)
    comments_branch = _branch(comments or [], fail_comments)

    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "gh"
        # Heredoc terminators must sit at column 0, so the case body is not
        # indented.
        script = (
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            '*"/issues/"*"/comments"*)\n'
            f"{comments_branch}\n;;\n"
            '*"/pulls/"*"/reviews"*)\n'
            f"{reviews_branch}\n;;\n"
            "esac\n"
        )
        stub.write_text(script)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = os.environ.copy()
        env["PATH"] = f"{tmp}:{env['PATH']}"
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"source {LIB}; discover_sentinel_sha owner repo 1 {login}",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        return result.stdout.strip(), result.returncode, result.stderr


def test_returns_sha_when_sentinel_present():
    sha, rc, _ = _run([_review(_sentinel_body(SENTINEL_SHA_A))])
    assert rc == 0
    assert sha == SENTINEL_SHA_A


def test_returns_empty_with_exit_1_when_no_sentinel():
    sha, rc, _ = _run([_review("review without a sentinel")])
    assert rc == 1
    assert sha == ""


def test_returns_empty_with_exit_1_when_no_reviews():
    sha, rc, _ = _run([])
    assert rc == 1
    assert sha == ""


def test_returns_empty_with_exit_2_on_api_failure():
    sha, rc, _ = _run("FAIL")
    assert rc == 2
    assert sha == ""


def test_log_err_includes_gh_stderr_on_failure():
    # The function must surface gh's stderr in the error log so operators can
    # tell a rate-limit from a 5xx from a DNS failure. Silencing it earlier
    # collapsed every failure into a single opaque "failed" line.
    msg = "HTTP 403: rate limit exceeded"
    sha, rc, stderr = _run("FAIL", fail_stderr=msg)
    assert rc == 2
    assert sha == ""
    assert msg in stderr


def test_picks_most_recent_sentinel_by_submitted_at():
    sha, rc, _ = _run(
        [
            _review(_sentinel_body(SENTINEL_SHA_A), submitted_at="2026-05-28T10:00:00Z"),
            _review(_sentinel_body(SENTINEL_SHA_B), submitted_at="2026-05-28T12:00:00Z"),
            _review(_sentinel_body(SENTINEL_SHA_C), submitted_at="2026-05-28T11:00:00Z"),
        ]
    )
    assert rc == 0
    assert sha == SENTINEL_SHA_B


def test_filters_by_login():
    # Only the other-operator review carries a sentinel; ours has none.
    # Should not return the other operator's SHA.
    sha, rc, _ = _run(
        [
            _review("ours without sentinel", login="operator"),
            _review(_sentinel_body(SENTINEL_SHA_A), login="someone-else"),
        ]
    )
    assert rc == 1
    assert sha == ""


def test_uses_created_at_for_pending_reviews():
    # Pending reviews have submitted_at=null. Sort falls back to created_at.
    sha, rc, _ = _run(
        [
            _review(
                _sentinel_body(SENTINEL_SHA_A),
                submitted_at="2026-05-28T08:00:00Z",
                created_at="2026-05-28T07:55:00Z",
            ),
            _review(
                _sentinel_body(SENTINEL_SHA_B),
                submitted_at=None,
                created_at="2026-05-28T12:00:00Z",
            ),
        ]
    )
    assert rc == 0
    assert sha == SENTINEL_SHA_B


def test_skips_reviews_without_sentinel_when_picking_most_recent():
    # Most recent review has no sentinel; fall through to the next-most-recent
    # one that does. Covers the realistic case where a sentinel-write regression
    # ships briefly and is reverted in the same PR.
    sha, rc, _ = _run(
        [
            _review(_sentinel_body(SENTINEL_SHA_A), submitted_at="2026-05-28T10:00:00Z"),
            _review("regression — no sentinel", submitted_at="2026-05-28T12:00:00Z"),
        ]
    )
    assert rc == 0
    assert sha == SENTINEL_SHA_A


def test_finds_sentinel_in_comment_when_review_body_wiped():
    # The #49 payoff: a blank-modal submit wiped the sentinel from the review
    # body, but the mirrored PR comment still carries it.
    sha, rc, _ = _run(
        reviews=[_review("")],
        comments=[_comment(_sentinel_body(SENTINEL_SHA_A))],
    )
    assert rc == 0
    assert sha == SENTINEL_SHA_A


def test_picks_most_recent_sentinel_across_reviews_and_comments():
    # Reviews and comments share one timeline; the newer comment wins.
    sha, rc, _ = _run(
        reviews=[_review(_sentinel_body(SENTINEL_SHA_A), submitted_at="2026-05-28T10:00:00Z")],
        comments=[_comment(_sentinel_body(SENTINEL_SHA_B), created_at="2026-05-28T12:00:00Z")],
    )
    assert rc == 0
    assert sha == SENTINEL_SHA_B


def test_comments_api_failure_degrades_to_reviews_only():
    # Reviews are primary: if only the comments call fails, still return the
    # review sentinel with rc 0 rather than escalating to the rc-2 skip.
    sha, rc, _ = _run(
        reviews=[_review(_sentinel_body(SENTINEL_SHA_A))],
        fail_comments=True,
    )
    assert rc == 0
    assert sha == SENTINEL_SHA_A


def test_comment_uses_updated_at_not_created_at():
    # ADR 0024 regression: the status comment is created once (old created_at)
    # then edited every tick, most recently with a fresh sentinel from a
    # zero-finding review that never got its own review-object sentinel. If
    # sorting used created_at, this comment would look ancient next to the
    # review below and the stale review sentinel would win, defeating the fix.
    sha, rc, _ = _run(
        reviews=[_review(_sentinel_body(SENTINEL_SHA_A), submitted_at="2026-05-28T10:00:00Z")],
        comments=[
            _comment(
                _sentinel_body(SENTINEL_SHA_B),
                created_at="2026-05-01T09:00:00Z",
                updated_at="2026-05-28T12:00:00Z",
            )
        ],
    )
    assert rc == 0
    assert sha == SENTINEL_SHA_B


def test_filters_comment_sentinel_by_login():
    # A sentinel in someone else's comment must not be mistaken for ours.
    sha, rc, _ = _run(
        reviews=[_review("ours, no sentinel", login="operator")],
        comments=[_comment(_sentinel_body(SENTINEL_SHA_A), login="someone-else")],
    )
    assert rc == 1
    assert sha == ""
