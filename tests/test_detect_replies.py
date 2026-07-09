"""Tests for daemon/detect-replies.jq — the unaddressed-Operator-reply selector (#39, #153).

reply-pr.sh feeds the PR's inline review comments through this filter to decide which
threads still need a daemon reply. The logic was an untested inline jq blob; that let
#153 ship, where a daemon-authored comment was mistaken for an Operator reply and
answered. These tests pin the selection against the real jq, including that the
Provenance tag excludes the daemon's own threaded comments.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
JQ_FILE = REPO_ROOT / "daemon" / "detect-replies.jq"
LIB = REPO_ROOT / "daemon" / "lib.sh"

LOGIN = "operator"
MARKER = "🤖 _pr-review-agent_"  # lib.sh PROVENANCE_TAG; pinned by test_provenance_tag


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


def _select_pages(*pages: list[dict]) -> list[dict]:
    """Run reply-pr.sh's composed pipeline on `gh api --paginate` wire output:
    one JSON array per page, concatenated (`[...][...]`), flattened by
    lib.sh's flatten_pages before the jq filter sees it (#195)."""
    out = subprocess.run(
        [
            "bash",
            "-c",
            f"source {LIB}; flatten_pages | "
            f"jq --arg login {LOGIN} --arg provenance '{MARKER}' -f {JQ_FILE}",
        ],
        input="".join(json.dumps(page) for page in pages),
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


def test_daemon_reply_ack_is_not_treated_as_an_operator_reply():
    # #153: a daemon reply ack is a daemon comment (Provenance tag) threaded under the
    # Finding. Its own id is not in the addressed-set, so the Provenance tag is the only
    # gate; without it the daemon would dispatch and answer its own ack.
    ack = _comment(
        4,
        in_reply_to=1,
        body=f"Thanks, confirmed.\n\n{MARKER}\n\n<!-- pr-review-agent:reply:2 -->",
    )
    assert _select([_finding(1), ack]) == []


def test_thread_split_across_pages_is_selected_once():
    # #195: without flattening, the jq filter ran once per page-document — an
    # acked thread on a mis-parsed page could re-dispatch, and the caller's
    # `jq length` count came out multi-line. The finding sits on page 1, its
    # operator reply on page 2; the acked thread spans pages too and must stay
    # excluded.
    acked_finding = _comment(10, body=f"**bug** other thing\n\n{MARKER}")
    acked_reply = _comment(11, in_reply_to=10, body="done <!-- pr-review-agent:reply:11 -->")
    live_reply = _comment(2, in_reply_to=1, body="thanks, fixed it")
    threads = _select_pages([_finding(1), acked_finding], [live_reply, acked_reply])
    assert len(threads) == 1
    assert threads[0]["operator_reply"]["comment_id"] == "2"


def test_operator_reply_selected_even_with_a_daemon_ack_present():
    # A real Operator reply alongside a daemon ack: only the human reply is picked.
    reply = _comment(2, in_reply_to=1, body="looks good now")
    ack = _comment(4, in_reply_to=1, body=f"Glad to hear it.\n\n{MARKER}")
    threads = _select([_finding(1), reply, ack])
    assert len(threads) == 1
    assert threads[0]["operator_reply"]["comment_id"] == "2"
