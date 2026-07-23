"""Round-trip + schema tests for daemon/create_reply.py (#36, #55, #79, #74).

The reply poster used to be a bash `python heredoc | jq | while read -r | gh`
pipeline. #36 collapses it to one Python process so the body bytes survive end
to end. #55 adds a per-thread Ack reaction. #79 embeds a Reply sentinel in the
parent Finding for non-claim threads so they dedup. #74 turns the `question`
bucket into a body-bearing answer (`stands`/`withdrawn`) that posts a text reply
like fix_claim, leaving `acknowledgment` as the only PATCH-dedup bucket. #159
posts each body-bearing reply detached (one `/replies` POST per ack, no review
wrapper); a settled verdict then stamps the Finding and resolves its thread.
These tests pin all of it: a body carrying `\n`, `\t`, `\\`, a backticked regex
`` `\n[^\n]` `` and non-ASCII must reach `gh` byte-for-byte over REST stdin, each
bucket must POST its reaction, a body-bearing bucket carries its sentinel in the
reply, and an acknowledgment PATCHes the parent Finding only once its reaction
lands.

`gh` is stubbed via a tmpdir on PATH (mirrors test_status_comment). The stub
records each call's argv and stdin as a NUL-delimited record (a reply body
carries newlines on stdin, so a newline separator would desync the two logs).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CREATE_REPLY = REPO_ROOT / "daemon" / "create_reply.py"

# Import create_reply as a module so its constants (the resolution sentinel) are
# reachable directly, alongside the subprocess _run tests below.
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("create_reply", CREATE_REPLY)
create_reply = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(create_reply)

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
# body carries no blob link: just the prose and the sentinel. Link assembly is
# exercised separately below.
EXPECTED_BODY = NASTY_BODY + "\n\n<!-- pr-review-agent:reply:222 -->"


def _raw(replies: list[dict]) -> str:
    """Wrap replies in the ```json fence the reply agent emits, with noise
    around it so the last-fence extraction is exercised."""
    return "agent preamble\n```json\n" + json.dumps({"replies": replies}) + "\n```\ntrailing prose"


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
) -> tuple[subprocess.CompletedProcess, list[tuple[str, str]]]:
    """Run create_reply.py with a per-call-recording `gh` stub. Returns
    (result, calls) where calls is a list of (argv, stdin) tuples in order.
    `threads` writes a --threads file supplying parent Finding bodies (#79).
    `head_sha`, when set, is passed as --head-sha so the blob link is built.
    `head_owner`/`head_repo` override the blob link's repo (the fork on a
    cross-repo PR); --owner/--repo stay the base repo for the API calls.

    `fail_ops` makes the stub exit 1 only for `gh` calls whose argv contains one
    of the given substrings (e.g. a GraphQL operation name or an endpoint), so a
    best-effort resolve failure can be tested without also failing the reply
    POST."""
    fail_ops = fail_ops or []
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        raw_file = tmpd / "raw.txt"
        raw_file.write_text(raw)
        stdin_log = tmpd / "gh_stdin.txt"
        argv_log = tmpd / "gh_argv.txt"
        stub = tmpd / "gh"
        # NUL-delimited record per invocation in each log, so argv[i] pairs with
        # stdin[i] even when a record carries newlines: a reply body posts via REST
        # `--input -` on stdin and contains `\n`, so a newline record separator
        # would desync the two logs.
        lines = [
            "#!/usr/bin/env bash",
            f'printf "%s\\0" "$*" >> "{argv_log}"',
            f'cat >> "{stdin_log}"',
            f'printf "\\0" >> "{stdin_log}"',
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
            str(CREATE_REPLY),
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


def _threads(
    finding_id: str,
    body: str,
    thread_id: str | None = None,
    comment_node_id: str | None = None,
) -> list[dict]:
    """A minimal --threads payload carrying one parent Finding body, keyed by
    the comment id a non-claim reply's `in_reply_to_id` points at. `thread_id`
    is the GraphQL review-thread node id reply-pr.sh joins in for resolution
    (#75); `comment_node_id` is the Finding comment's own GraphQL node id, joined
    in for the GraphQL resolution stamp (#163). Omit either to model a thread the
    reviewThreads map could not resolve."""
    pf = {"comment_id": finding_id, "body": body}
    if comment_node_id is not None:
        pf["comment_node_id"] = comment_node_id
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
    assert "🤖 _pr-review-agent_" not in body  # marker retired (ADR 0036)
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
    assert "🤖 _pr-review-agent_" not in body  # marker retired (ADR 0036)


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


def test_italic_confirmed_lead_renders_with_link_on_one_line():
    # #106: reply verdict leads are italic, peeled by voice.split_lead (#104), so
    # the verdict reads straight into the blob link on line one — `_Confirmed:_ [link]`.
    reply = _reply(body="_Confirmed:_", verified_path="daemon/lib.sh", verified_line=88)
    _, calls = _run(_raw([reply]), head_sha=HEAD_SHA)
    body = _reply_body(calls)
    link = f"[`a1b2c3d:L88`](https://github.com/example/example/blob/{HEAD_SHA}/daemon/lib.sh#L88)"
    assert body.startswith(f"_Confirmed:_ {link}"), body
    assert "🤖 _pr-review-agent_" not in body  # marker retired (ADR 0036)


def test_fix_claim_without_verified_fields_has_marker_but_no_link():
    # head sha present, but the agent emitted nothing to anchor (e.g. a fix
    # confirmed by deletion): marker still appended, no blob link.
    _, calls = _run(_raw([_reply()]), head_sha=HEAD_SHA)
    body = _reply_body(calls)
    assert "🤖 _pr-review-agent_" not in body  # marker retired (ADR 0036)
    assert "/blob/" not in body, body


def test_fix_claim_without_head_sha_has_no_link():
    # No --head-sha (older invocation path): the link cannot be built even with
    # verified fields present, but the marker still lands.
    reply = _reply(verified_path="daemon/lib.sh", verified_line=88)
    _, calls = _run(_raw([reply]))
    body = _reply_body(calls)
    assert "/blob/" not in body, body
    assert "🤖 _pr-review-agent_" not in body  # marker retired (ADR 0036)


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
    # degrades to just the lead and the sentinel footer.
    reply = _reply(body="**Confirmed by deletion.**")
    _, calls = _run(_raw([reply]), head_sha=HEAD_SHA)
    body = _reply_body(calls)
    assert body.startswith("**Confirmed by deletion.**\n\n<!-- pr-review-agent:reply:"), body


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
    assert "🤖 _pr-review-agent_" not in body  # marker retired (ADR 0036)


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


def test_confirmed_resolves_thread_after_reply_posts():
    # The reply posts detached (one /replies POST, no review wrapper, #159);
    # resolution runs only after that POST lands, so a thread never collapses off
    # an ack that never went live.
    result, calls = _run(_raw([_reply()]), threads=_threads("111", "F", thread_id="PRRT_abc"))
    assert result.returncode == 0, result.stderr
    reply = _find(calls, "pulls/999/comments/111/replies")
    assert reply is not None, "the reply posts detached"
    resolve = _resolve_call(calls)
    assert resolve is not None, "confirmed must resolve the thread"
    assert "threadId=PRRT_abc" in resolve[0]
    assert _idx(calls, "resolveReviewThread") > _idx(calls, "/replies"), (
        "resolve only after the reply posts"
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
    # The reply posts (and stamps) fine but only the resolve mutation fails. The
    # run still exits 0: resolution is cosmetic and never retried.
    result, calls = _run(
        _raw([_reply()]),
        fail_ops=["resolveReviewThread"],
        threads=_threads("111", "F", thread_id="PRRT_x", comment_node_id="PRRC_x"),
    )
    assert result.returncode == 0, result.stderr
    assert _find(calls, "/replies") is not None, "reply still posts"
    assert _resolve_call(calls) is not None, "resolve was attempted"


# ---------- #159 / ADR 0019: a resolving reply also stamps the Finding comment ----------
# #163: the stamp is written via the same GraphQL updatePullRequestReviewComment
# mutation the commit-driven path uses, keyed on the Finding comment's node id
# (joined in by reply-pr.sh). The bodiless sentinel PATCH stays on REST, so the
# stamp call is detected by the mutation name, never by `--method PATCH`.

RESOLVED_SENTINEL = create_reply.resolution.RESOLUTION_SENTINEL
FINDING_NODE_ID = "PRRC_kwDOcomment111"


def _stamp_call(calls: list[tuple[str, str]]) -> tuple[str, str] | None:
    """The GraphQL resolution-stamp write. The body rides in argv (`-f body=…`),
    not stdin, so callers assert against the argv record."""
    return _find(calls, "updatePullRequestReviewComment")


def test_confirmed_stamps_the_finding_via_graphql_node_id():
    # A confirmed verdict resolves the thread AND edits the Finding's own comment to
    # append the single resolution stamp (ADR 0019), on top of the threaded ack. The
    # stamp goes through GraphQL keyed on the comment node id (#163), unified with
    # the commit-driven path — never a REST PATCH.
    result, calls = _run(
        _raw([_reply()]),
        threads=_threads(
            "111",
            "Unbounded loop.",
            thread_id="PRRT_c",
            comment_node_id=FINDING_NODE_ID,
        ),
    )
    assert result.returncode == 0, result.stderr
    assert _resolve_call(calls) is not None, "confirmed resolves the thread"
    stamp = _stamp_call(calls)
    assert stamp is not None, "the Finding comment is stamped via GraphQL"
    assert f"commentId={FINDING_NODE_ID}" in stamp[0], "stamp is keyed on the comment node id"
    assert "✅ _Resolved_: confirmed fixed by the author" in stamp[0]
    assert RESOLVED_SENTINEL in stamp[0]
    # The edit appends below the original body, never clobbers it.
    assert "Unbounded loop." in stamp[0]
    assert _find(calls, "--method PATCH") is None, "the stamp is GraphQL, not a REST PATCH"


def test_withdrawn_stamp_carries_its_own_rationale():
    result, calls = _run(
        _raw([_question(mode="withdrawn")]),
        threads=_threads("111", "Off-by-one.", thread_id="PRRT_w", comment_node_id=FINDING_NODE_ID),
    )
    assert result.returncode == 0, result.stderr
    stamp = _stamp_call(calls)
    assert stamp is not None and "withdrawn by the author as a false positive" in stamp[0]


def test_confirmed_without_node_id_skips_stamp_but_still_resolves():
    # Degraded reviewThreads query: the thread id mapped but the comment node id did
    # not. The stamp is skipped (best-effort, like the no-thread-id resolve skip), yet
    # the reply still posts and the thread still resolves.
    result, calls = _run(_raw([_reply()]), threads=_threads("111", "F", thread_id="PRRT_n"))
    assert result.returncode == 0, result.stderr
    assert _find(calls, "/replies") is not None, "the reply still posts"
    assert _resolve_call(calls) is not None, "the thread still resolves"
    assert _stamp_call(calls) is None, "no node id -> the stamp is skipped"


def test_resolving_reply_does_not_re_stamp_an_already_stamped_finding():
    # Re-run safety: the Finding comment already carries a stamp (its earlier resolve
    # had dropped), so append_stamp returns None and no second edit is made.
    already = f"Off-by-one.\n\n✅ _Resolved_: confirmed fixed by the author\n\n{RESOLVED_SENTINEL}"
    _, calls = _run(
        _raw([_reply()]),
        threads=_threads("111", already, thread_id="PRRT_c", comment_node_id=FINDING_NODE_ID),
    )
    assert _stamp_call(calls) is None, "an already-stamped Finding is not re-edited"


def test_non_resolving_verdict_leaves_no_stamp():
    # pushback keeps the Finding live, so no resolution stamp is written.
    _, calls = _run(
        _raw([_reply(mode="pushback")]),
        threads=_threads("111", "F", thread_id="PRRT_p", comment_node_id=FINDING_NODE_ID),
    )
    assert _stamp_call(calls) is None, "a non-resolving verdict leaves no stamp"


def test_resolve_skipped_when_reply_post_fails():
    # If the reply POST fails, nothing lands and the next cycle retries; the thread
    # must not be resolved off a reply that never posted.
    _, calls = _run(
        _raw([_reply()]),
        gh_exit=1,
        threads=_threads("111", "F", thread_id="PRRT_x", comment_node_id=FINDING_NODE_ID),
    )
    assert _resolve_call(calls) is None, "no resolve when the reply never posted"
    assert _stamp_call(calls) is None, "no stamp when the reply never posted"


# --- Detached posting (#159) ------------------------------------------------
# Each body-bearing reply posts detached: one /replies POST per ack, no review
# wrapper. A reply-tick with N replies sends N notifications; the #38 wrapper that
# once batched them into one is retired (ADR 0019).


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


def test_each_reply_posts_detached_no_review_wrapper():
    # Two body-bearing replies in one tick: each posts as its own /replies POST,
    # and none of the #38 wrapper machinery (create/add/submit) is touched.
    a = _reply(in_reply_to_id="111", addressed_comment_id="11", body="**Confirmed.** a")
    b = _reply(in_reply_to_id="222", addressed_comment_id="22", body="**Confirmed.** b")
    result, calls = _run(
        _raw([a, b]),
        threads=_multi_threads(("111", "Fa", "PRRT_a"), ("222", "Fb", "PRRT_b")),
    )
    assert result.returncode == 0, result.stderr
    assert _find(calls, "pulls/999/comments/111/replies") is not None, "reply a posts detached"
    assert _find(calls, "pulls/999/comments/222/replies") is not None, "reply b posts detached"
    for op in ("CreatePendingReview", "AddReply", "SubmitReview"):
        assert _find(calls, op) is None, f"no review-wrapper machinery ({op})"


def test_detached_reply_carries_sentinel_byte_for_byte():
    # The dedup sentinel rides in the detached body and the nasty payload
    # round-trips over REST stdin intact. No provenance marker: the bot's own
    # login carries who-wrote-this now (ADR 0036).
    _, calls = _run(_raw([_reply()]), threads=_threads("111", "F", thread_id="PRRT_x"))
    body = _reply_body(calls)
    assert body == EXPECTED_BODY
    assert "<!-- pr-review-agent:reply:222 -->" in body, "reply sentinel rides in the body"
    assert "🤖 _pr-review-agent_" not in body, "the provenance marker is retired"


def test_dry_run_confirmed_includes_resolve_thread_id_and_stamp():
    result, _ = _run(
        _raw([_reply()]), dry_run=True, threads=_threads("111", "F", thread_id="PRRT_d")
    )
    assert result.returncode == 0, result.stderr
    entry = json.loads(result.stdout)["plan"][0]
    assert entry["resolve_thread_id"] == "PRRT_d"
    assert "✅ _Resolved_: confirmed fixed by the author" in entry["resolution_stamp"]


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
# with extract_json.py; the next polling cycle re-runs the reply agent.


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


def test_single_bullet_reply_body_is_style_violation():
    # 2b (#100): reply bodies hold to the same 0-or-2–4 bullet rule as Inline
    # comments. A lone bullet fails the batch before any POST.
    result, calls = _run(_raw([_reply(body="**Partial fix.**\n\n- only one point left")]))
    assert result.returncode == 1
    assert "category=style-violation" in result.stderr
    assert calls == []


def test_bulleted_reply_body_passes_the_voice_gate():
    body = "**Partial fix.**\n\n- the import is gone\n- the call site still emits"
    result, calls = _run(_raw([_reply(body=body)]))
    assert result.returncode == 0, result.stderr
    assert _find(calls, "/replies") is not None
