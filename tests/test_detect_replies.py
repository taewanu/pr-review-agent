"""Tests for daemon/detect-replies.jq — the unaddressed-Operator-reply selector (#39, #153).

reply-pr.sh feeds the PR's inline review comments through this filter to decide which
threads still need a daemon reply. The logic was an untested inline jq blob; that let
#153 ship, where the daemon's own `_Fixed:_` note (commit-driven resolution, #125) was
mistaken for an Operator fix-claim and answered `_Confirmed:_`. These tests pin the
selection against the real jq.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
JQ_FILE = REPO_ROOT / "daemon" / "detect-replies.jq"

LOGIN = "operator"
MARKER = "🤖 _pr-review-agent_"  # lib.sh PROVENANCE_TAG; pinned by test_provenance_tag
FIX_SENTINEL = "<!-- pr-review-agent:fixed -->"


def _comment(cid, *, in_reply_to=None, login=LOGIN, body="", path="a.py", line=9) -> dict:
    return {
        "id": cid,
        "in_reply_to_id": in_reply_to,
        "user": {"login": login},
        "body": body,
        "path": path,
        "line": line,
        "original_line": line,
    }


def _finding(cid=1) -> dict:
    """A daemon Finding (thread root): our login, carries the Provenance tag."""
    return _comment(cid, body=f"**bug** flag the thing\n\n{MARKER}")


def _select(comments: list[dict]) -> list[dict]:
    out = subprocess.run(
        ["jq", "--arg", "login", LOGIN, "--arg", "provenance", MARKER, "-f", str(JQ_FILE)],
        input=json.dumps(comments),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def test_operator_reply_is_selected():
    reply = _comment(2, in_reply_to=1, body="thanks, fixed it")
    threads = _select([_finding(1), reply])
    assert len(threads) == 1
    assert threads[0]["operator_reply"]["comment_id"] == "2"


def test_acked_reply_is_excluded():
    # A reply we already acked carries the Reply sentinel and must not re-dispatch.
    acked = _comment(2, in_reply_to=1, body="our ack <!-- pr-review-agent:reply:2 -->")
    assert _select([_finding(1), acked]) == []


def test_reply_to_a_non_daemon_finding_is_excluded():
    # Parent finding belongs to someone else, so it is not our thread to answer.
    other = _comment(1, login="someone-else", body="their comment")
    reply = _comment(2, in_reply_to=1, body="a reply")
    assert _select([other, reply]) == []


def test_fix_note_is_not_treated_as_an_operator_reply():
    # #153: a commit-driven `_Fixed:_` note is a daemon comment (Provenance tag +
    # fix sentinel, no reply sentinel). It must never be dispatched as a fix-claim.
    fix_note = _comment(
        4,
        in_reply_to=1,
        body=f"_Fixed:_ [link](url)\n\nguard added\n\n{MARKER}\n\n{FIX_SENTINEL}",
    )
    assert _select([_finding(1), fix_note]) == []


def test_operator_reply_selected_even_with_a_fix_note_present():
    # A real Operator reply alongside a `_Fixed:_` note: only the human reply is picked.
    reply = _comment(2, in_reply_to=1, body="looks good now")
    fix_note = _comment(
        4, in_reply_to=1, body=f"_Fixed:_ guard added\n\n{MARKER}\n\n{FIX_SENTINEL}"
    )
    threads = _select([_finding(1), reply, fix_note])
    assert len(threads) == 1
    assert threads[0]["operator_reply"]["comment_id"] == "2"
