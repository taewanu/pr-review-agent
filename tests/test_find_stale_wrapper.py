"""Tests for daemon/lib.sh's `find_stale_wrapper_review` (#125).

The review path's resolution stage must discard a stale fix-note wrapper (a prior
tick that added notes but failed to submit) before opening this tick's review, or
GitHub's one-pending-review-per-viewer rule blocks the create. This is the cleanup
the reply path already does in reply-pr.sh, lifted into a shared lib.sh helper so
the wrapper marker lives in one place.

The marker is the gate: the helper returns only a PENDING review by the operator
whose body carries the wrapper marker, so it never deletes a Finding-bearing
Pending review (the ADR 0008 safety gate) or a human reviewer's draft.

Tests stub `gh` via a tmpdir on PATH so the real jq pipeline runs offline.
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
REPLY_PR = REPO_ROOT / "daemon" / "reply-pr.sh"
BATCH_REVIEW = REPO_ROOT / "daemon" / "batch_review.py"

# The wrapper marker's stable inner key, matched as a substring (the same form
# reply-pr.sh uses). Pinned across all three sites by test_wrapper_marker_pinned.
MARKER_KEY = "pr-review-agent:reply-review"


def _review(rid: str, body: str, *, login: str = "operator") -> dict:
    return {"id": rid, "author": {"login": login}, "body": body}


def _run(reviews: list[dict] | str, operator: str = "operator") -> tuple[str, int]:
    """Invoke find_stale_wrapper_review with a stub `gh` for the GraphQL query.

    Pass reviews="FAIL" to make the query exit non-zero; the helper is best-effort
    and must echo nothing without erroring the caller."""
    fail = reviews == "FAIL"
    if fail:
        branch = "exit 1"
    else:
        payload = {"data": {"repository": {"pullRequest": {"reviews": {"nodes": reviews}}}}}
        branch = "cat <<'JSON_EOF'\n" + json.dumps(payload) + "\nJSON_EOF"

    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "gh"
        script = f'#!/usr/bin/env bash\ncase "$*" in\n*graphql*)\n{branch}\n;;\nesac\n'
        stub.write_text(script)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = os.environ.copy()
        env["PATH"] = f"{tmp}:{env['PATH']}"
        result = subprocess.run(
            ["bash", "-c", f"source {LIB}; find_stale_wrapper_review owner repo 1 {operator}"],
            capture_output=True,
            text=True,
            env=env,
        )
        return result.stdout.strip(), result.returncode


def _wrapper_body() -> str:
    return f"1 conversation resolved.\n\n<!-- {MARKER_KEY} -->"


def test_returns_id_of_operator_wrapper():
    rid, rc = _run([_review("PRR_stale", _wrapper_body())])
    assert rc == 0
    assert rid == "PRR_stale"


def test_empty_when_pending_review_has_no_marker():
    # A Finding-bearing Pending review (the ADR 0008 gate) carries no wrapper
    # marker, so it is never selected for deletion.
    rid, rc = _run([_review("PRR_finding", "real review body, no marker")])
    assert rc == 0
    assert rid == ""


def test_filters_by_operator_login():
    # A human reviewer's marked draft under a different login is left untouched.
    rid, rc = _run([_review("PRR_other", _wrapper_body(), login="someone-else")])
    assert rc == 0
    assert rid == ""


def test_empty_when_no_pending_reviews():
    rid, rc = _run([])
    assert rc == 0
    assert rid == ""


def test_query_failure_is_best_effort_empty():
    rid, rc = _run("FAIL")
    assert rc == 0
    assert rid == ""


def test_wrapper_marker_pinned_across_sites():
    # The wrapper marker has three producers/matchers now: batch_review.WRAPPER_MARKER
    # (the body it writes), reply-pr.sh (its own stale-wrapper jq), and lib.sh's
    # find_stale_wrapper_review. A drift would silently orphan one path's wrapper
    # from another's cleanup, re-wedging GitHub's one-pending rule.
    assert MARKER_KEY in BATCH_REVIEW.read_text()
    assert MARKER_KEY in REPLY_PR.read_text()
    assert MARKER_KEY in LIB.read_text()
