"""Tests for daemon/submit-review.sh (#63).

submit-review.sh finds the operator's PENDING review on a PR and submits it via
the GitHub events API (`POST .../reviews/:id/events`), omitting `body` so the
drafted summary survives. Per ADR 0008 this is the others'-PR submit path that
avoids the body-wiping web modal (#50).

`gh` is stubbed via a tmpdir on PATH (same approach as test_sentinel_discovery):
the stub dispatches by argument shape — the auth check, `api user`, the reviews
list, and the events POST. The events POST is recorded to a file so a live
submit can be asserted without touching the network.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "daemon" / "submit-review.sh"
PR_URL = "https://github.com/example/example/pull/7"


def _review(state: str, *, login: str = "operator", review_id: int = 101) -> dict:
    return {"id": review_id, "state": state, "user": {"login": login}}


def _run(
    reviews: list[dict],
    *extra_args: str,
    login: str = "operator",
    url: str = PR_URL,
) -> tuple[str, int, str, str]:
    """Run submit-review.sh with a stubbed gh. Returns (stdout, rc, events_calls,
    stderr) where events_calls is the recorded args of any POST .../events call.
    Log lines (log_info / log_err) go to stderr, so status messages land there."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        record = tmpdir / "events_calls.txt"
        stub = tmpdir / "gh"
        # Heredoc terminators sit at column 0, so the case bodies are not indented.
        # The events branch precedes the reviews branch because the events path
        # also contains "reviews".
        script = (
            "#!/usr/bin/env bash\n"
            f'record="{record}"\n'
            'case "$*" in\n'
            '*"auth status"*) exit 0 ;;\n'
            '*"reviews/"*"/events"*)\n'
            'printf "%s\\n" "$*" >>"$record"\n'
            "cat <<'JSON_EOF'\n" + json.dumps({"state": "COMMENTED"}) + "\nJSON_EOF\n;;\n"
            '*"/pulls/"*"/reviews"*)\n'
            "cat <<'JSON_EOF'\n" + json.dumps(reviews) + "\nJSON_EOF\n;;\n"
            '*"api user"*)\n'
            f'printf "%s\\n" "{login}" ;;\n'
            "esac\n"
        )
        stub.write_text(script)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = os.environ.copy()
        env["PATH"] = f"{tmpdir}:{env['PATH']}"
        result = subprocess.run(
            ["bash", str(SCRIPT), *extra_args, url],
            capture_output=True,
            text=True,
            env=env,
        )
        events = record.read_text() if record.exists() else ""
        return result.stdout.strip(), result.returncode, events, result.stderr


def test_dry_run_plans_comment_submit_for_pending():
    out, rc, events, _ = _run([_review("PENDING")], "--dry-run")
    assert rc == 0
    plan = json.loads(out)
    assert plan["review_id"] == 101
    assert plan["event"] == "COMMENT"
    assert plan["endpoint"] == "repos/example/example/pulls/7/reviews/101/events"
    assert events == ""  # dry-run never posts


def test_no_pending_review_is_noop():
    # Only a submitted review exists (e.g. an own-PR auto-submit), nothing pending.
    _, rc, events, err = _run([_review("COMMENTED")])
    assert rc == 0
    assert "no pending review" in err
    assert events == ""


def test_only_operator_pending_is_selected():
    # A pending review by someone else must not be submitted under the operator.
    reviews = [_review("PENDING", login="colleague", review_id=200), _review("COMMENTED")]
    _, rc, events, err = _run(reviews)
    assert rc == 0
    assert "no pending review" in err
    assert events == ""


def test_dry_run_event_override():
    out, rc, _, _ = _run([_review("PENDING")], "--event", "APPROVE", "--dry-run")
    assert rc == 0
    assert json.loads(out)["event"] == "APPROVE"


def test_live_submit_posts_event_without_body():
    # The body-wipe fix: the events POST carries `event` but never `body`, so the
    # drafted pending summary is preserved.
    _, rc, events, _ = _run([_review("PENDING")])
    assert rc == 0
    assert "repos/example/example/pulls/7/reviews/101/events" in events
    assert "event=COMMENT" in events
    assert "body" not in events


def test_invalid_event_rejected():
    _, rc, events, _ = _run([_review("PENDING")], "--event", "MERGE")
    assert rc == 1
    assert events == ""


def test_invalid_url_rejected():
    _, rc, _, _ = _run([_review("PENDING")], url="not-a-pr-url")
    assert rc == 1
