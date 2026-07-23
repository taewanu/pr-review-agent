"""Tests for daemon/detect-replies.jq — the unaddressed-reply selector (#39, #153).

reply-pr.sh feeds the PR's inline review comments through this filter to decide which
threads still need a reply. The logic was an untested inline jq blob; that let #153
ship, where the bot's own reply ack was mistaken for a human reply and answered. Under
App identity (ADR 0036) the gate is authorship: a reply is answered only when its
author is not a Bot, under one of the bot's own Findings. These tests pin that against
the real jq.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
JQ_FILE = REPO_ROOT / "daemon" / "detect-replies.jq"
LIB = REPO_ROOT / "daemon" / "lib.sh"

# The bot's REST login (`<slug>[bot]`), the author of every Finding this filter
# answers replies on. Neutral fixture value, not a real App slug.
BOT_LOGIN = "example[bot]"


def _comment(
    cid, *, in_reply_to=None, login="a-human", user_type="User", body="", path="a.py", line=9
) -> dict:
    return {
        "id": cid,
        "in_reply_to_id": in_reply_to,
        "user": {"login": login, "type": user_type},
        "body": body,
        "path": path,
        "line": line,
        "original_line": line,
    }


def _finding(cid=1) -> dict:
    """A Finding (thread root): authored by the bot."""
    return _comment(cid, login=BOT_LOGIN, user_type="Bot", body="**bug** flag the thing")


def _ack(cid, *, in_reply_to, body) -> dict:
    """The bot's own reply ack: a Bot author threaded under a Finding."""
    return _comment(cid, in_reply_to=in_reply_to, login=BOT_LOGIN, user_type="Bot", body=body)


def _select(comments: list[dict]) -> list[dict]:
    out = subprocess.run(
        ["jq", "--arg", "login", BOT_LOGIN, "-f", str(JQ_FILE)],
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
            f"source {LIB}; flatten_pages | jq --arg login '{BOT_LOGIN}' -f {JQ_FILE}",
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


def test_bot_reply_ack_is_not_treated_as_a_human_reply():
    # #153: the bot's own reply ack is threaded under the Finding. Under App
    # identity it posts as a Bot, so user.type excludes it; without that gate the
    # bot would dispatch and answer its own ack. No addressed-set entry is needed.
    ack = _ack(4, in_reply_to=1, body="Thanks, confirmed.")
    assert _select([_finding(1), ack]) == []


def test_thread_split_across_pages_is_selected_once():
    # #195: without flattening, the jq filter ran once per page-document — an
    # acked thread on a mis-parsed page could re-dispatch, and the caller's
    # `jq length` count came out multi-line. The finding sits on page 1, its
    # operator reply on page 2; the acked thread spans pages too and must stay
    # excluded.
    acked_finding = _comment(10, login=BOT_LOGIN, user_type="Bot", body="**bug** other thing")
    acked_reply = _comment(11, in_reply_to=10, body="done <!-- pr-review-agent:reply:11 -->")
    live_reply = _comment(2, in_reply_to=1, body="thanks, fixed it")
    threads = _select_pages([_finding(1), acked_finding], [live_reply, acked_reply])
    assert len(threads) == 1
    assert threads[0]["operator_reply"]["comment_id"] == "2"


def test_human_reply_selected_even_with_a_bot_ack_present():
    # A real human reply alongside the bot's own ack: only the human reply is picked.
    reply = _comment(2, in_reply_to=1, body="looks good now")
    ack = _ack(4, in_reply_to=1, body="Glad to hear it.")
    threads = _select([_finding(1), reply, ack])
    assert len(threads) == 1
    assert threads[0]["operator_reply"]["comment_id"] == "2"
