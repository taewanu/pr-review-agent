"""Round-trip + schema tests for daemon/post_reply.py (#36, #55, #79, #74).

The reply poster used to be a bash `python heredoc | jq | while read -r | gh`
pipeline. #36 collapses it to one Python process so the body bytes survive end
to end. #55 adds a per-thread Ack reaction. #79 embeds a Reply sentinel in the
parent Finding for non-claim threads so they dedup. #74 turns the `question`
bucket into a body-bearing answer (`stands`/`withdrawn`) that posts a text reply
like fix_claim, leaving `acknowledgment` as the only PATCH-dedup bucket. #38
wraps body-bearing replies in one pending COMMENTED review per tick (GraphQL
create -> add-per-thread -> submit) instead of N detached `/replies`, falling
back to the detached REST path for any reply without a thread id. These tests pin
all of it: a body carrying `\n`, `\t`, `\\`, a backticked regex `` `\n[^\n]` ``
and non-ASCII must reach `gh` byte-for-byte over BOTH transports (REST stdin and
the `-f body=` GraphQL arg), each bucket must POST its reaction, a body-bearing
bucket carries its sentinel in the reply, and an acknowledgment PATCHes the
parent Finding only once its reaction lands.

`gh` is stubbed via a tmpdir on PATH (mirrors test_status_comment). The stub
records each call's argv and stdin as a NUL-delimited record (a batched body
carries newlines on argv, so a newline separator would desync the two logs) and
feeds a canned id back for the CreatePendingReview mutation.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POST_REPLY = REPO_ROOT / "daemon" / "post_reply.py"

# The chars the old bash pipeline could mangle: newline, tab, backslash, a
# backticked regex with a literal `\n`, and non-ASCII.
NASTY_BODY = (
    "first line\n"
    "second\twith a tab\n"
    "a literal backslash \\ here\n"
    "regex `\\n[^\\n]` in backticks\n"
    "unicode 안녕 café"
)

# Default _run() passes no --head-sha and no verified_* fields, so a fix_claim
# body carries no blob link: just the prose, the provenance marker, and the
# sentinel. Link assembly is exercised separately below.
EXPECTED_BODY = NASTY_BODY + "\n\n🤖 _pr-review-agent_\n\n<!-- pr-review-agent:reply:222 -->"


def _raw(replies: list[dict]) -> str:
    """Wrap replies in the ```json fence the reply agent emits, with noise
    around it so the last-fence extraction is exercised."""
    return "agent preamble\n```json\n" + json.dumps({"replies": replies}) + "\n```\ntrailing prose"


# Canned CreatePendingReview response so post_reply.py can read the new review id
# off the stub (#38). Returned for any `gh` call whose argv contains the mutation
# operation name.
_CREATE_REVIEW_STDOUT = '{"data":{"addPullRequestReview":{"pullRequestReview":{"id":"PRR_stub"}}}}'


def _run(
    raw: str,
    *,
    dry_run: bool = False,
    gh_exit: int = 0,
    fail_ops: list[str] | None = None,
    threads: list[dict] | None = None,
    head_sha: str | None = None,
    head_owner: str | None = None,
    head_repo: str | None = None,
    pr_node_id: str | None = "PR_node",
    existing_pending_review_id: str | None = None,
) -> tuple[subprocess.CompletedProcess, list[tuple[str, str]]]:
    """Run post_reply.py with a per-call-recording `gh` stub. Returns
    (result, calls) where calls is a list of (argv, stdin) tuples in order.
    `threads` writes a --threads file supplying parent Finding bodies (#79).
    `head_sha`, when set, is passed as --head-sha so the blob link is built.
    `head_owner`/`head_repo` override the blob link's repo (the fork on a
    cross-repo PR); --owner/--repo stay the base repo for the API calls.

    `pr_node_id` (default present) is passed as --pr-node-id so a body reply with
    a thread id batches under one pending COMMENTED review (#38); set it to None
    to model a degraded reviewThreads read that forces the detached REST path. The
    stub feeds a canned id back for the CreatePendingReview mutation so add/submit
    have a review to attach to. `fail_ops` makes the stub exit 1 only for `gh`
    calls whose argv contains one of the given substrings (e.g. a GraphQL
    operation name), so a best-effort resolve failure can be tested without also
    failing the create/add/submit."""
    fail_ops = fail_ops or []
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        raw_file = tmpd / "raw.txt"
        raw_file.write_text(raw)
        stdin_log = tmpd / "gh_stdin.txt"
        argv_log = tmpd / "gh_argv.txt"
        stub = tmpd / "gh"
        # NUL-delimited record per invocation in each log, so argv[i] pairs with
        # stdin[i] even when an arg carries newlines: the batch path (#38) passes
        # the reply body via `-f body=<raw>` on argv (not stdin), and that body
        # contains `\n`, so a newline record separator would desync the two logs.
        lines = [
            "#!/usr/bin/env bash",
            f'printf "%s\\0" "$*" >> "{argv_log}"',
            f'cat >> "{stdin_log}"',
            f'printf "\\0" >> "{stdin_log}"',
            f"case \"$*\" in *CreatePendingReview*) printf '%s' '{_CREATE_REVIEW_STDOUT}' ;; esac",
        ]
        if fail_ops:
            clauses = " ".join(f'*"{op}"*) exit 1 ;;' for op in fail_ops)
            lines.append(f'case "$*" in {clauses} esac')
        lines.append(f"exit {gh_exit}")
        stub.write_text("\n".join(lines) + "\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = os.environ.copy()
        env["PATH"] = f"{tmpd}:{env['PATH']}"
        args = [
            "python3",
            str(POST_REPLY),
            "--owner",
            "example",
            "--repo",
            "example",
            "--number",
            "999",
            "--raw",
            str(raw_file),
        ]
        if threads is not None:
            threads_file = tmpd / "threads.json"
            threads_file.write_text(json.dumps(threads))
            args += ["--threads", str(threads_file)]
        if head_sha is not None:
            args += ["--head-sha", head_sha]
        if head_owner is not None:
            args += ["--head-owner", head_owner]
        if head_repo is not None:
            args += ["--head-repo", head_repo]
        if pr_node_id is not None:
            args += ["--pr-node-id", pr_node_id]
        if existing_pending_review_id is not None:
            args += ["--existing-pending-review-id", existing_pending_review_id]
        if dry_run:
            args.append("--dry-run")
        result = subprocess.run(args, capture_output=True, text=True, env=env)
        # Each call appends one NUL-terminated record, so split drops the trailing
        # empty field after the final NUL.
        argv_recs = argv_log.read_text().split("\0")[:-1] if argv_log.exists() else []
        stdin_recs = stdin_log.read_text().split("\0")[:-1] if stdin_log.exists() else []
        calls = list(zip(argv_recs, stdin_recs, strict=True))
        return result, calls


def _reply(**over) -> dict:
    base = {
        "in_reply_to_id": "111",
        "addressed_comment_id": "222",
        "bucket": "fix_claim",
        "mode": "confirmed",
        "body": NASTY_BODY,
    }
    base.update(over)
    return base


def _question(**over) -> dict:
    """An answered question (#74): a body-bearing bucket like fix_claim, but its
    mode is stands/withdrawn. Same posting path as _reply (text reply + sentinel
    in the reply), distinct only in the per-bucket mode value-set."""
    base = {
        "in_reply_to_id": "111",
        "addressed_comment_id": "222",
        "bucket": "question",
        "mode": "stands",
        "body": NASTY_BODY,
    }
    base.update(over)
    return base


def _find(calls: list[tuple[str, str]], needle: str) -> tuple[str, str] | None:
    return next((c for c in calls if needle in c[0]), None)


def _threads(finding_id: str, body: str, thread_id: str | None = None) -> list[dict]:
    """A minimal --threads payload carrying one parent Finding body, keyed by
    the comment id a non-claim reply's `in_reply_to_id` points at. `thread_id`
    is the GraphQL review-thread node id reply-pr.sh joins in for resolution
    (#75); omit it to model a thread the reviewThreads map could not resolve."""
    pf = {"comment_id": finding_id, "body": body}
    thread: dict = {
        "parent_finding": pf,
        "operator_reply": {"comment_id": "222", "body": "..."},
    }
    if thread_id is not None:
        thread["thread_id"] = thread_id
    return [thread]


def test_fix_claim_round_trips_body_and_posts_eyes_reaction():
    result, calls = _run(_raw([_reply()]))
    assert result.returncode == 0, result.stderr

    reply_call = _find(calls, "pulls/999/comments/111/replies")
    assert reply_call is not None, "fix_claim must POST a threaded reply"
    assert json.loads(reply_call[1])["body"] == EXPECTED_BODY

    react_call = _find(calls, "pulls/comments/222/reactions")
    assert react_call is not None, "fix_claim must react on the operator comment"
    assert json.loads(react_call[1]) == {"content": "eyes"}


HEAD_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


def _reply_body(calls: list[tuple[str, str]]) -> str:
    reply_call = _find(calls, "/replies")
    assert reply_call is not None
    return json.loads(reply_call[1])["body"]


def test_fix_claim_builds_blob_link_for_a_range():
    reply = _reply(verified_path="daemon/lib.sh", verified_line=88, verified_end_line=95)
    _, calls = _run(_raw([reply]), head_sha=HEAD_SHA)
    body = _reply_body(calls)
    expected = f"[`a1b2c3d:L88-L95`](https://github.com/example/example/blob/{HEAD_SHA}/daemon/lib.sh#L88-L95)"
    assert expected in body, body
    assert "🤖 _pr-review-agent_" in body
    # Location lives in the link only — the prose body carried no coordinates.
    assert body.startswith(NASTY_BODY)


def test_fix_claim_builds_blob_link_for_a_single_line():
    reply = _reply(verified_path="auth/session.py", verified_line=42)
    _, calls = _run(_raw([reply]), head_sha=HEAD_SHA)
    body = _reply_body(calls)
    expected = (
        f"[`a1b2c3d:L42`](https://github.com/example/example/blob/{HEAD_SHA}/auth/session.py#L42)"
    )
    assert expected in body, body


def test_pushback_builds_blob_link_too():
    reply = _reply(mode="pushback", verified_path="daemon/poll.sh", verified_line=115)
    _, calls = _run(_raw([reply]), head_sha=HEAD_SHA)
    body = _reply_body(calls)
    assert f"/blob/{HEAD_SHA}/daemon/poll.sh#L115" in body, body
    assert "🤖 _pr-review-agent_" in body


def test_blob_link_targets_head_repo_not_base():
    # Cross-repo PR: the comment API calls hit the base repo (example/example),
    # but the blob link must point at the head repo where head_sha lives, or it
    # 404s.
    reply = _reply(verified_path="daemon/lib.sh", verified_line=88)
    _, calls = _run(_raw([reply]), head_sha=HEAD_SHA, head_owner="forker", head_repo="thefork")
    body = _reply_body(calls)
    assert f"https://github.com/forker/thefork/blob/{HEAD_SHA}/daemon/lib.sh#L88" in body, body
    assert "example/example/blob" not in body, body
    # The threaded reply itself still POSTs to the base repo's PR.
    assert _find(calls, "repos/example/example/pulls/999/comments") is not None


def test_fix_claim_without_verified_fields_has_marker_but_no_link():
    # head sha present, but the agent emitted nothing to anchor (e.g. a fix
    # confirmed by deletion): marker still appended, no blob link.
    _, calls = _run(_raw([_reply()]), head_sha=HEAD_SHA)
    body = _reply_body(calls)
    assert "🤖 _pr-review-agent_" in body
    assert "/blob/" not in body, body


def test_fix_claim_without_head_sha_has_no_link():
    # No --head-sha (older invocation path): the link cannot be built even with
    # verified fields present, but the marker still lands.
    reply = _reply(verified_path="daemon/lib.sh", verified_line=88)
    _, calls = _run(_raw([reply]))
    body = _reply_body(calls)
    assert "/blob/" not in body, body
    assert "🤖 _pr-review-agent_" in body


# A voice-compliant reply body: a bold verdict lead, then explanation prose. The
# real agent always leads with bold (validated via strip_bold); NASTY_BODY above
# drops it to stress byte-safety, so the verdict-lead layout (#96) is pinned here.
LEAD_BODY = "**Confirmed.** The guard now covers the null case."


def test_reply_leads_with_verdict_and_link_then_prose_below():
    # #96: the bold lead and the blob link share the first line; the explanation
    # drops to its own paragraph below, never repeating the coordinates.
    reply = _reply(body=LEAD_BODY, verified_path="daemon/lib.sh", verified_line=88)
    _, calls = _run(_raw([reply]), head_sha=HEAD_SHA)
    body = _reply_body(calls)
    link = f"[`a1b2c3d:L88`](https://github.com/example/example/blob/{HEAD_SHA}/daemon/lib.sh#L88)"
    assert body.startswith(f"**Confirmed.** {link}\n\n"), body
    assert "\n\nThe guard now covers the null case.\n\n" in body, body


def test_reply_without_link_keeps_lead_then_prose():
    # No verified line to anchor: the lead sits alone on the first line and the
    # explanation drops below, with no link jammed in.
    reply = _reply(body=LEAD_BODY)
    _, calls = _run(_raw([reply]), head_sha=HEAD_SHA)
    body = _reply_body(calls)
    assert body.startswith("**Confirmed.**\n\nThe guard now covers the null case.\n\n"), body
    assert "/blob/" not in body, body


def test_reply_lead_only_no_prose_degrades_to_lead_plus_footer():
    # Confirmed-by-deletion style: a bold lead with nothing after it and no link
    # degrades to just the lead and the footer.
    reply = _reply(body="**Confirmed by deletion.**")
    _, calls = _run(_raw([reply]), head_sha=HEAD_SHA)
    body = _reply_body(calls)
    assert body.startswith("**Confirmed by deletion.**\n\n🤖 _pr-review-agent_"), body


def test_acknowledgment_posts_plus_one_reaction_only():
    reply = {"in_reply_to_id": "1", "addressed_comment_id": "222", "bucket": "acknowledgment"}
    result, calls = _run(_raw([reply]), threads=_threads("1", "FINDING"))
    assert result.returncode == 0, result.stderr
    assert _find(calls, "/replies") is None, "acknowledgment posts no text reply"
    react_call = _find(calls, "pulls/comments/222/reactions")
    assert react_call is not None
    assert json.loads(react_call[1]) == {"content": "+1"}


def test_question_stands_posts_text_reply_and_eyes_reaction():
    # #74: an answered question is a body-bearing bucket. It POSTs a text reply
    # carrying its sentinel and reacts eyes. The parent Finding is NOT PATCHed
    # even though a parent body is available, because the sentinel rides in the
    # reply (only acknowledgment uses the PATCH path now).
    result, calls = _run(_raw([_question()]), threads=_threads("111", "FINDING"))
    assert result.returncode == 0, result.stderr

    reply_call = _find(calls, "pulls/999/comments/111/replies")
    assert reply_call is not None, "an answered question must POST a threaded reply"
    assert json.loads(reply_call[1])["body"] == EXPECTED_BODY

    react_call = _find(calls, "pulls/comments/222/reactions")
    assert react_call is not None
    assert json.loads(react_call[1]) == {"content": "eyes"}

    assert _find(calls, "--method PATCH") is None, (
        "a question carries its sentinel in the reply, not a parent-Finding PATCH"
    )


def test_question_withdrawn_posts_a_text_concession():
    # withdrawn is a text reply too, never emoji-only: the operator must be able
    # to tell a conceded finding from a plain "thanks" ack.
    reply = _question(
        mode="withdrawn", body="**You're right, false positive.** Already masked at L84."
    )
    result, calls = _run(_raw([reply]))
    assert result.returncode == 0, result.stderr
    reply_call = _find(calls, "/replies")
    assert reply_call is not None, "a withdrawn question still posts a concession"
    body = json.loads(reply_call[1])["body"]
    assert body.startswith("**You're right, false positive.**")
    assert "<!-- pr-review-agent:reply:222 -->" in body
    assert _find(calls, "--method PATCH") is None


def test_question_builds_blob_link_like_a_fix_claim():
    # stands anchors the line that still shows the problem; the link path is the
    # same builder fix_claim uses.
    reply = _question(verified_path="daemon/lib.sh", verified_line=88)
    _, calls = _run(_raw([reply]), head_sha=HEAD_SHA)
    body = _reply_body(calls)
    assert f"/blob/{HEAD_SHA}/daemon/lib.sh#L88" in body, body
    assert "🤖 _pr-review-agent_" in body


def test_question_mode_defaults_to_stands():
    # No `mode` key on a question — default is the "holds" verdict (mirrors
    # fix_claim defaulting to confirmed), and it still posts a reply.
    reply = {"in_reply_to_id": "1", "addressed_comment_id": "2", "bucket": "question", "body": "x"}
    result, calls = _run(_raw([reply]))
    assert result.returncode == 0, result.stderr
    assert "1 thread(s): 0 fix-claim, 1 question, 0 acknowledgment" in result.stderr
    assert _find(calls, "/replies") is not None, "a question is body-bearing, so it posts a reply"


def test_dry_run_question_has_reply_payload_not_sentinel_patch():
    result, _ = _run(_raw([_question()]), dry_run=True)
    entry = json.loads(result.stdout)["plan"][0]
    assert entry["bucket"] == "question"
    assert entry["reaction"] == "eyes"
    assert "sentinel_patch" not in entry
    assert entry["reply_payload"]["body"] == EXPECTED_BODY


def test_non_claim_embeds_reply_sentinel_in_parent_finding():
    # The reaction lands, so the parent Finding is PATCHed with the captured
    # body plus the Reply sentinel keyed on the operator reply id.
    reply = {"in_reply_to_id": "1", "addressed_comment_id": "222", "bucket": "acknowledgment"}
    result, calls = _run(_raw([reply]), threads=_threads("1", "FINDING body"))
    assert result.returncode == 0, result.stderr
    patch_call = _find(calls, "--method PATCH")
    assert patch_call is not None, "non-claim must PATCH the parent finding"
    assert "pulls/comments/1" in patch_call[0]
    assert json.loads(patch_call[1])["body"] == "FINDING body\n\n<!-- pr-review-agent:reply:222 -->"


def test_non_claim_failed_reaction_skips_sentinel_patch():
    # The reaction is the thread's only ack, so a failed reaction must not leave
    # a sentinel behind — the next cycle retries the reaction first.
    reply = {"in_reply_to_id": "1", "addressed_comment_id": "222", "bucket": "acknowledgment"}
    result, calls = _run(_raw([reply]), gh_exit=1, threads=_threads("1", "FINDING body"))
    assert result.returncode == 0
    assert _find(calls, "reactions") is not None, "reaction is attempted"
    assert _find(calls, "--method PATCH") is None, "no sentinel PATCH when the reaction fails"


def test_two_non_claims_on_one_finding_accumulate_both_sentinels():
    # Two acknowledgments on the same Finding in one cycle: the second PATCH
    # appends to the body the first one extended, so neither sentinel is lost.
    # (Only acknowledgment takes the PATCH path now; a question would post a
    # reply instead, see test_question_stands_*.)
    replies = [
        {"in_reply_to_id": "1", "addressed_comment_id": "222", "bucket": "acknowledgment"},
        {"in_reply_to_id": "1", "addressed_comment_id": "333", "bucket": "acknowledgment"},
    ]
    result, calls = _run(_raw(replies), threads=_threads("1", "FINDING body"))
    assert result.returncode == 0, result.stderr
    patches = [c for c in calls if "--method PATCH" in c[0]]
    assert len(patches) == 2
    final_body = json.loads(patches[-1][1])["body"]
    assert "<!-- pr-review-agent:reply:222 -->" in final_body
    assert "<!-- pr-review-agent:reply:333 -->" in final_body


# --- Thread resolution (#75) ----------------------------------------------
# A settled verdict (confirmed / withdrawn) leaves nothing actionable, so the
# daemon resolves the GitHub review thread via GraphQL after the reply lands.
# pushback / stands / acknowledgment keep the thread open. Best-effort: a resolve
# failure is logged, never retried (the reply already carried the sentinel).


def _resolve_call(calls: list[tuple[str, str]]) -> tuple[str, str] | None:
    return _find(calls, "resolveReviewThread")


def _idx(calls: list[tuple[str, str]], needle: str) -> int:
    return next(i for i, c in enumerate(calls) if needle in c[0])


def test_confirmed_resolves_thread_after_review_submits():
    # Under the review wrapper (#38) the reply is an AddReply under the pending
    # review, not a detached /replies POST; resolution still runs, but only after
    # the review submits (the replies and their sentinels are not live until then).
    result, calls = _run(_raw([_reply()]), threads=_threads("111", "F", thread_id="PRRT_abc"))
    assert result.returncode == 0, result.stderr
    assert _find(calls, "AddReply") is not None, "the reply batches under the review"
    assert _find(calls, "/replies") is None, "no detached reply when batched"
    resolve = _resolve_call(calls)
    assert resolve is not None, "confirmed must resolve the thread"
    assert "threadId=PRRT_abc" in resolve[0]
    assert _idx(calls, "resolveReviewThread") > _idx(calls, "SubmitReview"), (
        "resolve only after the review submits"
    )


def test_withdrawn_resolves_thread():
    reply = _question(mode="withdrawn")
    result, calls = _run(_raw([reply]), threads=_threads("111", "F", thread_id="PRRT_w"))
    assert result.returncode == 0, result.stderr
    resolve = _resolve_call(calls)
    assert resolve is not None, "withdrawn must resolve the thread"
    assert "threadId=PRRT_w" in resolve[0]


def test_pushback_does_not_resolve():
    reply = _reply(mode="pushback")
    _, calls = _run(_raw([reply]), threads=_threads("111", "F", thread_id="PRRT_p"))
    assert _resolve_call(calls) is None, "pushback keeps the Finding live -> open"


def test_stands_does_not_resolve():
    reply = _question(mode="stands")
    _, calls = _run(_raw([reply]), threads=_threads("111", "F", thread_id="PRRT_s"))
    assert _resolve_call(calls) is None, "stands keeps the Finding live -> open"


def test_acknowledgment_does_not_resolve():
    reply = {"in_reply_to_id": "111", "addressed_comment_id": "222", "bucket": "acknowledgment"}
    _, calls = _run(_raw([reply]), threads=_threads("111", "F", thread_id="PRRT_a"))
    assert _resolve_call(calls) is None, "acknowledgment carries no verdict -> open"


def test_confirmed_without_thread_id_skips_resolve_but_still_replies():
    # reply-pr.sh could not map a thread id (reviewThreads query degraded). The
    # reply still posts; resolution is skipped, not fatal.
    result, calls = _run(_raw([_reply()]), threads=_threads("111", "F"))
    assert result.returncode == 0, result.stderr
    assert _find(calls, "/replies") is not None, "reply still posts"
    assert _resolve_call(calls) is None, "no thread id -> no resolve"


def test_resolve_failure_is_best_effort_exit_zero():
    # The review submits (create/add/submit succeed) but only the resolve mutation
    # fails. The run still exits 0: resolution is cosmetic and never retried.
    result, calls = _run(
        _raw([_reply()]),
        fail_ops=["resolveReviewThread"],
        threads=_threads("111", "F", thread_id="PRRT_x"),
    )
    assert result.returncode == 0, result.stderr
    assert _find(calls, "AddReply") is not None, "reply still batches"
    assert _resolve_call(calls) is not None, "resolve was attempted"


def test_resolve_skipped_when_review_create_fails():
    # If the pending-review create fails, nothing lands and the next cycle retries;
    # the thread must not be resolved off a reply that never posted.
    _, calls = _run(_raw([_reply()]), gh_exit=1, threads=_threads("111", "F", thread_id="PRRT_x"))
    assert _resolve_call(calls) is None, "no resolve when the review never opened"


# --- Review-wrapper batching (#38) ------------------------------------------
# Body-bearing replies post under ONE pending COMMENTED review per tick so the
# operator gets a single notification, instead of N detached /replies comments.
# Threading is preserved: each AddReply targets its parent thread id. A reply
# with no thread id (degraded read / no pr node id) falls back to a detached
# /replies POST so it still lands.


def _multi_threads(*specs: tuple[str, str, str]) -> list[dict]:
    """A --threads payload for several findings: each spec is
    (finding_comment_id, body, thread_id)."""
    return [
        {
            "parent_finding": {"comment_id": cid, "body": body},
            "operator_reply": {"comment_id": "999", "body": "..."},
            "thread_id": tid,
        }
        for cid, body, tid in specs
    ]


def _addreply_body(rec: str) -> str:
    """The reply body carried on an AddReply call's argv (`-f body=<raw>`), which
    is the last `-f` arg so it runs to the end of the record."""
    return rec.split("body=", 1)[1]


def test_batch_wraps_replies_in_one_review_no_detached_posts():
    a = _reply(in_reply_to_id="111", addressed_comment_id="11", body="**Confirmed.** a")
    b = _reply(in_reply_to_id="222", addressed_comment_id="22", body="**Confirmed.** b")
    result, calls = _run(
        _raw([a, b]),
        threads=_multi_threads(("111", "Fa", "PRRT_a"), ("222", "Fb", "PRRT_b")),
    )
    assert result.returncode == 0, result.stderr
    assert len([c for c in calls if "CreatePendingReview" in c[0]]) == 1, "one review per tick"
    assert len([c for c in calls if "SubmitReview" in c[0]]) == 1, "one submit per tick"
    adds = [c for c in calls if "AddReply" in c[0]]
    assert len(adds) == 2, "each reply attaches to its own thread under the review"
    assert _find(calls, "thread=PRRT_a") is not None
    assert _find(calls, "thread=PRRT_b") is not None
    assert _find(calls, "/replies") is None, "nothing posts as a detached reply"


def test_batch_submits_as_comment_with_empty_body():
    _, calls = _run(_raw([_reply()]), threads=_threads("111", "F", thread_id="PRRT_x"))
    submit = _find(calls, "SubmitReview")
    assert submit is not None
    assert "event: COMMENT" in submit[0], "wrapper review submits as COMMENT"
    # #38 ships an empty wrapper body: the submit mutation sets no `body` field
    # (the deprecation/summary lives in #11), so no body variable is passed.
    assert "-f body=" not in submit[0] and "body=" not in submit[0].split("query=", 1)[1]


def test_batch_reply_body_round_trips_through_graphql_arg():
    # The body now travels via `-f body=<raw>` on argv, a new transport vs the REST
    # stdin path. It must still reach gh byte-for-byte: `\n`, tab, backslash, a
    # backticked regex and non-ASCII.
    _, calls = _run(_raw([_reply()]), threads=_threads("111", "F", thread_id="PRRT_x"))
    add = _find(calls, "AddReply")
    assert add is not None
    assert _addreply_body(add[0]) == EXPECTED_BODY


def test_null_thread_id_falls_back_to_detached_reply():
    batched = _reply(in_reply_to_id="111", addressed_comment_id="11", body="**Confirmed.** a")
    stray = _reply(in_reply_to_id="222", addressed_comment_id="22", body="**Confirmed.** b")
    result, calls = _run(
        _raw([batched, stray]),
        # only 111 has a thread id; 222's reviewThreads entry was not mapped
        threads=_multi_threads(("111", "Fa", "PRRT_a")) + _threads("222", "Fb"),
    )
    assert result.returncode == 0, result.stderr
    assert _find(calls, "thread=PRRT_a") is not None, "the mapped reply batches"
    detached = _find(calls, "pulls/999/comments/222/replies")
    assert detached is not None, "the unmapped reply posts detached so it still lands"


def test_no_pr_node_id_forces_detached_even_with_thread_id():
    # A fully degraded reviewThreads read leaves no pr node id, so batching is off
    # and every reply posts detached (the pre-#38 path) — still threaded, just N
    # notifications.
    result, calls = _run(
        _raw([_reply()]), threads=_threads("111", "F", thread_id="PRRT_x"), pr_node_id=None
    )
    assert result.returncode == 0, result.stderr
    assert _find(calls, "CreatePendingReview") is None, "no review without a pr node id"
    assert _find(calls, "pulls/999/comments/111/replies") is not None, "posts detached instead"


def test_stale_pending_review_deleted_before_create():
    result, calls = _run(
        _raw([_reply()]),
        threads=_threads("111", "F", thread_id="PRRT_x"),
        existing_pending_review_id="PRR_stale",
    )
    assert result.returncode == 0, result.stderr
    delete = _find(calls, "DeletePendingReview")
    assert delete is not None, "a stale pending review is discarded first"
    assert "review=PRR_stale" in delete[0]
    assert _idx(calls, "DeletePendingReview") < _idx(calls, "CreatePendingReview"), (
        "delete the stale review before opening this tick's review"
    )


def test_no_stale_pending_review_no_delete():
    _, calls = _run(_raw([_reply()]), threads=_threads("111", "F", thread_id="PRRT_x"))
    assert _find(calls, "DeletePendingReview") is None, "nothing to delete when none was passed"


def test_wrapper_review_tagged_with_reply_review_marker():
    # The wrapper body carries the hidden marker reply-pr.sh filters on, so only a
    # reply wrapper is ever deleted — never a Finding-bearing draft (the bug the
    # daemon flagged on #95).
    _, calls = _run(_raw([_reply()]), threads=_threads("111", "F", thread_id="PRRT_x"))
    create = _find(calls, "CreatePendingReview")
    assert create is not None
    assert "pr-review-agent:reply-review" in create[0], "wrapper body carries the discriminator"


def test_submit_failure_defers_and_skips_resolve():
    # The review fails to submit: the replies never go live, so the thread is not
    # resolved and the run still exits 0 (next cycle retries the whole batch).
    result, calls = _run(
        _raw([_reply()]),
        fail_ops=["SubmitReview"],
        threads=_threads("111", "F", thread_id="PRRT_x"),
    )
    assert result.returncode == 0, result.stderr
    assert _find(calls, "AddReply") is not None, "the add was attempted"
    assert _resolve_call(calls) is None, "no resolve when the review did not submit"
    assert "review submit failed" in result.stderr


def test_all_adds_fail_discards_empty_review_without_submit():
    # Every AddReply fails, so there is nothing to submit; the empty pending review
    # is discarded rather than submitted (an empty COMMENTED review is rejected).
    result, calls = _run(
        _raw([_reply()]),
        fail_ops=["AddReply"],
        threads=_threads("111", "F", thread_id="PRRT_x"),
    )
    assert result.returncode == 0, result.stderr
    assert _find(calls, "SubmitReview") is None, "never submit an empty review"
    assert _find(calls, "DeletePendingReview") is not None, "discard the empty review"


def test_acknowledgment_stays_out_of_the_batch():
    # An acknowledgment carries no body, so it never joins the review wrapper: it
    # still gets its reaction + parent-Finding sentinel PATCH, and a co-occurring
    # fix_claim is the only thing batched.
    ack = {"in_reply_to_id": "111", "addressed_comment_id": "11", "bucket": "acknowledgment"}
    fix = _reply(in_reply_to_id="222", addressed_comment_id="22", body="**Confirmed.** x")
    result, calls = _run(
        _raw([ack, fix]),
        threads=_multi_threads(("111", "Fa", "PRRT_a"), ("222", "Fb", "PRRT_b")),
    )
    assert result.returncode == 0, result.stderr
    adds = [c for c in calls if "AddReply" in c[0]]
    assert len(adds) == 1 and _find(calls, "thread=PRRT_b") is not None, (
        "only the fix_claim batches"
    )
    assert _find(calls, "thread=PRRT_a") is None, "the acknowledgment is not added to the review"
    assert _find(calls, "--method PATCH") is not None, (
        "the acknowledgment still PATCHes its sentinel"
    )


def test_batched_reply_keeps_sentinel_and_marker():
    # Dedup + provenance must survive the move into the review wrapper.
    _, calls = _run(_raw([_reply()]), threads=_threads("111", "F", thread_id="PRRT_x"))
    body = _addreply_body(_find(calls, "AddReply")[0])
    assert "<!-- pr-review-agent:reply:222 -->" in body, "reply sentinel rides in the batched body"
    assert "🤖 _pr-review-agent_" in body, "provenance marker rides in the batched body"


def test_dry_run_marks_batched_reply_via_review():
    result, _ = _run(
        _raw([_reply()]), dry_run=True, threads=_threads("111", "F", thread_id="PRRT_d")
    )
    entry = json.loads(result.stdout)["plan"][0]
    assert entry["via"] == "review"


def test_dry_run_marks_unmapped_reply_via_detached():
    result, _ = _run(_raw([_reply()]), dry_run=True, threads=_threads("111", "F"))
    entry = json.loads(result.stdout)["plan"][0]
    assert entry["via"] == "detached"


def test_dry_run_confirmed_includes_resolve_thread_id():
    result, _ = _run(
        _raw([_reply()]), dry_run=True, threads=_threads("111", "F", thread_id="PRRT_d")
    )
    assert result.returncode == 0, result.stderr
    entry = json.loads(result.stdout)["plan"][0]
    assert entry["resolve_thread_id"] == "PRRT_d"


def test_dry_run_emits_plan_without_calling_gh():
    result, calls = _run(_raw([_reply()]), dry_run=True)
    assert result.returncode == 0, result.stderr
    assert calls == [], "dry-run must not invoke gh"
    plan = json.loads(result.stdout)["plan"]
    assert plan[0]["bucket"] == "fix_claim"
    assert plan[0]["reaction"] == "eyes"
    assert plan[0]["in_reply_to_id"] == "111"
    assert plan[0]["reply_payload"]["body"] == EXPECTED_BODY


def test_dry_run_non_claim_has_reaction_and_sentinel_patch():
    reply = {"in_reply_to_id": "1", "addressed_comment_id": "222", "bucket": "acknowledgment"}
    result, _ = _run(_raw([reply]), dry_run=True)
    assert result.returncode == 0, result.stderr
    entry = json.loads(result.stdout)["plan"][0]
    assert entry["reaction"] == "+1"
    assert "reply_payload" not in entry
    assert entry["sentinel_patch"]["finding_id"] == "1"
    assert entry["sentinel_patch"]["sentinel"] == "<!-- pr-review-agent:reply:222 -->"


def test_mode_defaults_to_confirmed_for_fix_claim():
    # No `mode` key — #37 default applies and the count line reflects the bucket.
    reply = {"in_reply_to_id": "1", "addressed_comment_id": "2", "bucket": "fix_claim", "body": "x"}
    result, _ = _run(_raw([reply]), dry_run=True)
    assert result.returncode == 0, result.stderr
    assert "1 thread(s): 1 fix-claim, 0 question, 0 acknowledgment" in result.stderr


def test_empty_replies_exits_zero_without_gh():
    result, calls = _run(_raw([]))
    assert result.returncode == 0
    assert calls == [], "no threads means no POST"


def test_reaction_post_failure_is_best_effort_exit_zero():
    # A failed reaction POST must not abort the polling cycle (best-effort,
    # matching the text-reply path).
    result, _ = _run(_raw([_reply()]), gh_exit=1)
    assert result.returncode == 0
    assert "reply POST failed" in result.stderr
    assert "reaction POST failed" in result.stderr


def test_no_fence_is_no_fence_category():
    result, _ = _run("the agent said no JSON at all")
    assert result.returncode == 1
    assert "category=no-fence" in result.stderr


def test_invalid_json_is_parse_error_category():
    result, _ = _run("```json\n{not: valid, json,}\n```")
    assert result.returncode == 1
    assert "category=parse-error" in result.stderr


def test_missing_required_key_is_schema_invalid():
    reply = {"in_reply_to_id": "1", "bucket": "acknowledgment"}  # no addressed_comment_id
    result, _ = _run(_raw([reply]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


def test_bad_bucket_is_schema_invalid():
    result, _ = _run(_raw([_reply(bucket="bogus")]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


def test_fix_claim_missing_body_is_schema_invalid():
    reply = {"in_reply_to_id": "1", "addressed_comment_id": "2", "bucket": "fix_claim"}
    result, _ = _run(_raw([reply]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


def test_bad_mode_is_schema_invalid():
    result, _ = _run(_raw([_reply(mode="bogus")]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


def test_question_missing_body_is_schema_invalid():
    reply = {
        "in_reply_to_id": "1",
        "addressed_comment_id": "2",
        "bucket": "question",
        "mode": "stands",
    }
    result, _ = _run(_raw([reply]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


def test_question_rejects_a_fix_claim_mode():
    # confirmed/pushback belong to fix_claim; a question must use stands/withdrawn.
    result, _ = _run(_raw([_question(mode="confirmed")]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


def test_fix_claim_rejects_a_question_mode():
    result, _ = _run(_raw([_reply(mode="stands")]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


def test_empty_body_is_schema_invalid():
    # A whitespace-only body passes key-presence but would post nothing under the
    # "has body" discriminator, so the contract rejects it for body buckets.
    result, _ = _run(_raw([_reply(body="   ")]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


def test_question_empty_body_is_schema_invalid():
    result, _ = _run(_raw([_question(body="")]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


# --- voice enforcement parity (ADR 0010) -------------------------------------
# Reply bodies are validated at the extraction gate with the same rules as
# Inline comments. A violation fails the whole batch before any POST, symmetric
# with extract-json.py; the next polling cycle re-runs the reply agent.


def test_em_dash_in_reply_body_is_style_violation():
    result, calls = _run(_raw([_reply(body="**Confirmed.** Fixed it now — finally.")]))
    assert result.returncode == 1
    assert "category=style-violation" in result.stderr
    assert calls == [], "a voice violation fails the batch before any POST"


def test_forbidden_opener_in_reply_body_is_style_violation():
    # Bold lead peeled before the opener scan: `**This …**` trips like `This …`.
    result, calls = _run(_raw([_reply(body="**This still drops the guard.**")]))
    assert result.returncode == 1
    assert "category=style-violation" in result.stderr
    assert calls == []


def test_task_ref_in_reply_body_is_style_violation():
    result, calls = _run(_raw([_question(body="**Holds.** Same shape as the Phase 5 note.")]))
    assert result.returncode == 1
    assert "category=style-violation" in result.stderr
    assert calls == []


def test_one_bad_reply_fails_the_whole_batch():
    good = _reply(body="**Confirmed.** The guard now matches HEAD.")
    bad = _reply(addressed_comment_id="333", in_reply_to_id="444", body="Rename — now.")
    result, calls = _run(_raw([good, bad]))
    assert result.returncode == 1
    assert "category=style-violation" in result.stderr
    assert calls == [], "no reply posts when any reply in the batch violates voice"


def test_clean_bold_lead_reply_passes_the_voice_gate():
    result, calls = _run(_raw([_reply(body="**Confirmed.** The guard now matches HEAD.")]))
    assert result.returncode == 0, result.stderr
    assert _find(calls, "/replies") is not None, "a clean reply still posts"
