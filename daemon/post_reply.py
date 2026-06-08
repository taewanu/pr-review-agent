"""Single-process reply poster (#36).

reply-pr.sh used to build reply bodies through bash -> jq -> `while read -r` ->
gh: three layers where a missing `read -r` or a stray `jq -r` silently mangles
`\\n` / `\\t` / `\\` / backticked-regex payloads. This builds the JSON in one
process and hands it to `gh --input -`, so the body bytes stay intact end to
end; tests/test_post_reply.py pins the round-trip.

Each processed thread also gets an Ack reaction on the operator's reply
comment, chosen by the agent's classification bucket: fix-claims and questions
read as "seen / verifying" (eyes), acknowledgments read as "noted, no action"
(+1). The reaction POST is idempotent on GitHub (200 if the same user+content
reaction already exists, 201 if newly created), so re-running a polling cycle
cannot double-react and we skip a dedup GET entirely.

An acknowledgment carries no text reply, so its dedup marker is the Reply
sentinel embedded in the parent Finding's comment body via a silent PATCH, gated
on the Ack reaction landing first: the reaction is that thread's only ack, so it
must win before we mark the reply processed. fix_claim and answered questions
(#74) keep their sentinel in the text reply. Parent Finding bodies come from
--threads (the same JSON the reply agent consumed), so the PATCH needs no GET.

Body-bearing replies post under ONE pending COMMENTED review per tick (#38)
instead of N detached `/replies` comments, so the operator gets a single GitHub
notification. The body now travels via GraphQL `-f body=<raw>` rather than REST
`--input -`, but gh JSON-encodes the variable, so the bytes still survive intact
(the round-trip test pins both transports). A reply with no thread id (degraded
reviewThreads read, or a thread past the first-100 page) falls back to the
detached REST `/replies` POST so it still lands. The wrapper review carries an
empty body; its summary is deferred to #11.

A settled verdict (`confirmed` / `withdrawn`) also resolves its GitHub review
thread via GraphQL after the review submits (#75), using the thread id
reply-pr.sh joined into --threads. Best-effort: a failed resolve is logged,
never retried.

On failure, stderr carries a `category=<x>` line (no-fence, parse-error,
schema-invalid) so reply-pr.sh's log_failure mapping is unchanged.

Usage:
  python3 post_reply.py --owner O --repo R --number N --raw RAWFILE \
    [--threads THREADSFILE] [--pr-node-id NODEID] \
    [--existing-pending-review-id ID] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# daemon/ is not a package and this script is run by path, so add its own dir to
# the import path before importing the shared voice rules (ADR 0010).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice  # noqa: E402

REQUIRED = ("in_reply_to_id", "addressed_comment_id", "bucket")

# Classification buckets. fix_claim and question carry a text reply (mode +
# body, #74); acknowledgment is a reaction-only ack. The per-bucket mode
# value-set lives in BUCKET_MODES; its first value is the "holds" default the
# agent gets when it omits `mode` (fix_claim → confirmed #37, question → stands
# #74). acknowledgment has no mode.
VALID_BUCKETS = ("fix_claim", "question", "acknowledgment")
BUCKET_MODES = {
    "fix_claim": ("confirmed", "pushback"),
    "question": ("stands", "withdrawn"),
}

# Verdicts that leave nothing actionable on the thread, so the daemon resolves
# the GitHub review thread after the reply lands (#75): `confirmed` (the fix
# landed) and `withdrawn` (the Finding was retracted as a false positive).
# `pushback` and `stands` keep the Finding live; `acknowledgment` carries no
# verdict — all three leave the thread open. Resolution is a user-facing GitHub
# state change, orthogonal to the Reply sentinel (CONTEXT.md "Thread resolution").
RESOLVE_MODES = ("confirmed", "withdrawn")

# Single-line so the test gh-stub records the call on one argv line. GraphQL is
# whitespace-insensitive, so this is equivalent to the pretty form.
RESOLVE_MUTATION = (
    "mutation($threadId: ID!) { resolveReviewThread(input: {threadId: $threadId}) "
    "{ thread { id isResolved } } }"
)

# Review-wrapper batching (#38). Body-bearing replies post under ONE pending
# COMMENTED review per tick instead of N detached `/replies` comments, so the
# operator gets one GitHub notification. Each named so the test gh-stub can match
# the call by operation name (CreatePendingReview's response carries the new
# review id the adds and submit need). addPullRequestReviewThreadReply attaches a
# reply to the pending review AND to its parent thread, preserving threading; the
# `PRRT_` thread id is the same one reply-pr.sh joins for #75 resolution.
CREATE_REVIEW_MUTATION = (
    "mutation CreatePendingReview($pr: ID!) { "
    "addPullRequestReview(input: {pullRequestId: $pr}) { pullRequestReview { id } } }"
)
ADD_REPLY_MUTATION = (
    "mutation AddReply($review: ID!, $thread: ID!, $body: String!) { "
    "addPullRequestReviewThreadReply(input: {pullRequestReviewId: $review, "
    "pullRequestReviewThreadId: $thread, body: $body}) { comment { id } } }"
)
# Submit as COMMENT with no `body` field — the wrapper review carries no summary
# (#38 ships an empty body; the reply substance lives in each inner comment).
SUBMIT_REVIEW_MUTATION = (
    "mutation SubmitReview($review: ID!) { "
    "submitPullRequestReview(input: {pullRequestReviewId: $review, event: COMMENT}) "
    "{ pullRequestReview { id state } } }"
)
# Discards a stale pending review (a prior tick that added replies but failed to
# submit). GitHub allows one pending review per viewer per PR, so a leftover
# would block this tick's create; deleting it also drops its orphan comments,
# which are invisible to the REST scan and would otherwise re-trigger duplicate
# replies next cycle.
DELETE_REVIEW_MUTATION = (
    "mutation DeletePendingReview($review: ID!) { "
    "deletePullRequestReview(input: {pullRequestReviewId: $review}) "
    "{ pullRequestReview { id } } }"
)

# Ack reaction per bucket. GitHub's reaction set is fixed to
# +1/-1/laugh/confused/heart/hooray/rocket/eyes, so the design note's 🙏 is not
# postable; +1 is the "noted" marker. fix_claim/question read as "seen".
BUCKET_REACTION = {
    "fix_claim": "eyes",
    "question": "eyes",
    "acknowledgment": "+1",
}

# Reply sentinel. `{id}` is the operator reply's comment id, so the next polling
# cycle's detection (#39) skips that reply. Carried in a body-bearing reply
# (fix_claim or answered question) or, for an acknowledgment, in the parent
# Finding's comment body (#79).
SENTINEL = "<!-- pr-review-agent:reply:{id} -->"

# Provenance marker appended to every daemon text reply (#11). Answers "who
# wrote this" under the shared solo identity (ADR 0003). Not "drafted": a posted
# reply is never a draft. The trailing `_` closes the markdown italic; it carries
# no colon, so the sentinel scan in reply-pr.sh never false-matches it.
MARKER = "🤖 _pr-review-agent_"


class PayloadError(Exception):
    """Carries a log_failure category so reply-pr.sh can classify the exit."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def extract_payload(raw: str) -> dict:
    """Parse the reply agent's stdout into a validated {"replies": [...]} dict.

    Same fence convention as extract-json.py: the last ```json block wins.
    Every thread the agent processed appears here tagged with its `bucket`.
    `mode` and `body` are required for the body-bearing buckets (fix_claim and
    question, #74); `mode`'s value-set is per-bucket (BUCKET_MODES) and defaults
    to that bucket's "holds" verdict. An empty body is rejected: under the "has
    body" poster discriminator it would silently post nothing.
    """
    matches = re.findall(r"```json\s*\n(.*?)\n```", raw, re.DOTALL)
    if not matches:
        raise PayloadError("no-fence", "reply agent: no ```json fence in output")
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        raise PayloadError("parse-error", f"reply agent: JSON decode failed: {exc}") from exc

    if "replies" not in data or not isinstance(data["replies"], list):
        raise PayloadError("schema-invalid", "reply agent: missing or non-list 'replies' key")

    # Voice violations are collected across all body-bearing replies and raised
    # once after the loop, so the whole batch fails before any POST (ADR 0010),
    # symmetric with extract-json.py. Schema errors above still abort eagerly and
    # take precedence: a malformed payload never reaches the voice gate.
    style_violations: list[str] = []
    for i, r in enumerate(data["replies"]):
        missing = [k for k in REQUIRED if k not in r]
        if missing:
            raise PayloadError(
                "schema-invalid", f"reply agent: replies[{i}] missing keys: {missing}"
            )
        bucket = r["bucket"]
        if bucket not in VALID_BUCKETS:
            raise PayloadError(
                "schema-invalid",
                f"reply agent: replies[{i}] bucket {bucket!r} not in {VALID_BUCKETS}",
            )
        if bucket in BUCKET_MODES:
            body = r.get("body")
            if not (isinstance(body, str) and body.strip()):
                raise PayloadError(
                    "schema-invalid",
                    f"reply agent: replies[{i}] {bucket} missing or empty 'body'",
                )
            valid = BUCKET_MODES[bucket]
            mode = r.setdefault("mode", valid[0])
            if mode not in valid:
                raise PayloadError(
                    "schema-invalid",
                    f"reply agent: replies[{i}] {bucket} mode {mode!r} not in {valid}",
                )
            # Reply bodies lead with a bold sentence like Inline comments, so
            # peel it before the opener scan (strip_bold).
            style_violations += voice.check_text(
                body,
                prefixes=voice.FORBIDDEN_PREFIXES,
                strip_bold=True,
                label=f"replies[{i}].body",
            )

    if style_violations:
        raise PayloadError("style-violation", "reply agent: " + "; ".join(style_violations))
    return data


def build_link(
    owner: str,
    repo: str,
    head_sha: str,
    path: str | None,
    line: object,
    end_line: object = None,
) -> str | None:
    """A blob-at-HEAD deep link to the verified line(s), or None when there is
    nothing to anchor (no head sha, or the agent emitted no line, for example a
    confirmed-by-deletion).

    `owner`/`repo` must be the **head** repo: `head_sha` is a commit in the fork,
    so a base-repo blob URL 404s on a cross-repo PR. The daemon reply asserts the
    file's *current* state, so it points at the blob at HEAD (#11), not a
    per-commit diff. The anchor is GitHub's plain `#L<n>` blob form; the sha256
    path hash is the per-commit *diff* anchor and does not apply here. The label
    shows a short sha plus line so the destination reads without hovering; the URL
    carries the full sha for stability."""
    if not (head_sha and path and line):
        return None
    try:
        start = int(line)
        end = int(end_line) if end_line else None
    except (TypeError, ValueError):
        return None
    if end and end != start:
        frag = f"L{start}-L{end}"
        label = f"{head_sha[:7]}:L{start}-L{end}"
    else:
        frag = f"L{start}"
        label = f"{head_sha[:7]}:L{start}"
    url = f"https://github.com/{owner}/{repo}/blob/{head_sha}/{path}#{frag}"
    return f"[`{label}`]({url})"


def link_for(args: argparse.Namespace, reply: dict) -> str | None:
    """The blob-at-HEAD link for one body-bearing reply, from its `verified_*`
    fields plus the head repo and head sha. None when nothing to anchor. The link
    targets the head repo (where `head_sha` lives), falling back to the base
    owner/repo when not supplied so same-repo PRs and older callers still link."""
    return build_link(
        args.head_owner or args.owner,
        args.head_repo or args.repo,
        args.head_sha,
        reply.get("verified_path"),
        reply.get("verified_line"),
        reply.get("verified_end_line"),
    )


def build_body(body: str, addressed_id: str, link: str | None = None) -> str:
    """Agent body, the optional blob-at-HEAD link, the provenance marker, and the
    Reply sentinel footer, joined byte-for-byte. Location lives in the link, not
    the prose, so the agent body never repeats the file and line."""
    parts = [body]
    if link:
        parts.append(link)
    parts.append(MARKER)
    parts.append(SENTINEL.format(id=addressed_id))
    return "\n\n".join(parts)


def post_reply(
    owner: str, repo: str, number: str, in_reply_to_id: str, body: str
) -> tuple[int, str]:
    """POST one threaded reply. /comments/{id}/replies inherits path+line from
    the parent, so body is the only field. The payload is built with json.dumps
    and fed on stdin via `--input -` — no shell-quoting of the body anywhere."""
    payload = json.dumps({"body": body})
    endpoint = f"repos/{owner}/{repo}/pulls/{number}/comments/{in_reply_to_id}/replies"
    proc = subprocess.run(
        ["gh", "api", "--method", "POST", endpoint, "--input", "-"],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stderr


def post_reaction(owner: str, repo: str, comment_id: str, content: str) -> tuple[int, str]:
    """POST an Ack reaction on the operator's reply comment. Idempotent on
    GitHub's side (keyed on user+content), so a blind POST every cycle is safe
    and needs no GET-reactions dedup."""
    payload = json.dumps({"content": content})
    endpoint = f"repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions"
    proc = subprocess.run(
        ["gh", "api", "--method", "POST", endpoint, "--input", "-"],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stderr


def patch_finding(owner: str, repo: str, finding_id: str, body: str) -> tuple[int, str]:
    """PATCH a Finding's Inline comment body to embed the Reply sentinel for a
    non-claim thread. The sentinel rides in a comment we own (the parent
    Finding), invisible in rendered markdown, so the next polling cycle's
    detection skips the reply. Same idiom as lib.sh's status-comment edit; body
    is the full replacement, so callers pass the captured body plus footer."""
    payload = json.dumps({"body": body})
    endpoint = f"repos/{owner}/{repo}/pulls/comments/{finding_id}"
    proc = subprocess.run(
        ["gh", "api", "--method", "PATCH", endpoint, "--input", "-"],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stderr


def resolve_thread(thread_id: str) -> tuple[int, str]:
    """Resolve a GitHub review thread via the GraphQL resolveReviewThread
    mutation (#75). Idempotent on GitHub's side — safe on an already-resolved
    thread. Best-effort: callers log a failure and move on, never retrying. The
    reply already carried the Reply sentinel, so the next cycle will not revisit
    this thread; a failed resolve just leaves it open (the pre-#75 state).
    `input=""` closes stdin: gh reads the query from argv, not stdin."""
    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={RESOLVE_MUTATION}", "-f", f"threadId={thread_id}"],
        input="",
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stderr


def _graphql(query: str, **variables: str) -> subprocess.CompletedProcess:
    """Run one `gh api graphql` mutation. Variables go through `-f name=value`, so
    gh JSON-encodes each value — a reply body with `\\n` / backticks / non-ASCII
    survives byte-for-byte without any shell quoting (same guarantee as the REST
    `--input -` path). `input=""` closes stdin since the query rides in argv."""
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        args += ["-f", f"{name}={value}"]
    return subprocess.run(args, input="", capture_output=True, text=True, check=False)


def create_pending_review(pr_node_id: str) -> tuple[int, str | None, str]:
    """Open a pending COMMENTED review on the PR (#38). Returns
    (returncode, review_node_id, stderr); review_node_id is None when the call
    failed or the response did not carry an id, so the caller defers the batch to
    the next cycle rather than posting replies into a review that never opened."""
    proc = _graphql(CREATE_REVIEW_MUTATION, pr=pr_node_id)
    if proc.returncode != 0:
        return proc.returncode, None, proc.stderr
    try:
        review_id = json.loads(proc.stdout)["data"]["addPullRequestReview"]["pullRequestReview"][
            "id"
        ]
    except (json.JSONDecodeError, KeyError, TypeError):
        return proc.returncode, None, proc.stdout
    return proc.returncode, review_id, proc.stderr


def add_thread_reply(review_id: str, thread_id: str, body: str) -> tuple[int, str]:
    """Attach one reply to the pending review and its parent thread (#38)."""
    proc = _graphql(ADD_REPLY_MUTATION, review=review_id, thread=thread_id, body=body)
    return proc.returncode, proc.stderr


def submit_review(review_id: str) -> tuple[int, str]:
    """Submit the pending review as COMMENT — one notification for the tick."""
    proc = _graphql(SUBMIT_REVIEW_MUTATION, review=review_id)
    return proc.returncode, proc.stderr


def delete_pending_review(review_id: str) -> tuple[int, str]:
    """Discard a pending review (a stale one before creating, or this tick's own
    review when nothing was added so we never submit an empty COMMENTED review)."""
    proc = _graphql(DELETE_REVIEW_MUTATION, review=review_id)
    return proc.returncode, proc.stderr


def main() -> int:
    parser = argparse.ArgumentParser(description="Post threaded reply acks for reply-pr.sh (#36).")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--number", required=True)
    parser.add_argument("--raw", required=True, help="reply agent stdout capture")
    parser.add_argument(
        "--head-sha",
        default="",
        help="PR HEAD sha; the blob-at-HEAD reply link is built against it (#11)",
    )
    parser.add_argument(
        "--head-owner",
        default="",
        help="head repo owner for the blob link (the fork on a cross-repo PR); defaults to --owner",
    )
    parser.add_argument(
        "--head-repo",
        default="",
        help="head repo name for the blob link; defaults to --repo",
    )
    parser.add_argument(
        "--threads",
        help="threads JSON the reply agent consumed; supplies parent Finding "
        "bodies for the non-claim Reply-sentinel PATCH",
    )
    parser.add_argument(
        "--pr-node-id",
        default="",
        help="PR GraphQL node id; body-bearing replies batch under one pending "
        "COMMENTED review against it (#38). Empty -> every reply falls back to a "
        "detached REST reply (the pre-#38 path), so a degraded reviewThreads read "
        "still posts.",
    )
    parser.add_argument(
        "--existing-pending-review-id",
        default="",
        help="a viewer pending review already on the PR (a prior tick that failed "
        "to submit); deleted before opening this tick's review so the create is not "
        "blocked and orphan comments do not re-trigger duplicate replies (#38)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the actions that would be posted, do not call gh",
    )
    args = parser.parse_args()

    raw = Path(args.raw).read_text()
    try:
        data = extract_payload(raw)
    except PayloadError as exc:
        print(f"category={exc.category}", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 1

    # Parent Finding bodies keyed by comment id (== a reply's in_reply_to_id),
    # so a non-claim PATCH appends the sentinel to the captured body with no GET.
    # thread_ids carries the GraphQL review-thread node id reply-pr.sh joined in
    # (#75), keyed the same way, so a settled verdict resolves its thread. A
    # thread whose id could not be mapped (degraded reviewThreads query) is
    # absent here and skips resolution.
    finding_bodies: dict[str, str] = {}
    thread_ids: dict[str, str] = {}
    if args.threads:
        for t in json.loads(Path(args.threads).read_text()):
            pf = t.get("parent_finding") or {}
            cid = pf.get("comment_id")
            if cid is not None:
                finding_bodies[str(cid)] = pf.get("body") or ""
                if t.get("thread_id"):
                    thread_ids[str(cid)] = t["thread_id"]

    replies = data["replies"]
    fix = sum(1 for r in replies if r["bucket"] == "fix_claim")
    question = sum(1 for r in replies if r["bucket"] == "question")
    ack = sum(1 for r in replies if r["bucket"] == "acknowledgment")
    print(
        f"{len(replies)} thread(s): {fix} fix-claim, {question} question, {ack} acknowledgment",
        file=sys.stderr,
    )
    if not replies:
        return 0

    if args.dry_run:
        plan = []
        for r in replies:
            bucket = r["bucket"]
            addressed_id = str(r["addressed_comment_id"])
            in_reply_to_id = str(r["in_reply_to_id"])
            entry = {
                "addressed_comment_id": addressed_id,
                "bucket": bucket,
                "reaction": BUCKET_REACTION[bucket],
            }
            if r.get("body"):
                entry["in_reply_to_id"] = in_reply_to_id
                # "review" -> wrapped in the one pending COMMENTED review (#38);
                # "detached" -> a standalone REST reply (degraded read / no thread id).
                entry["via"] = (
                    "review" if args.pr_node_id and thread_ids.get(in_reply_to_id) else "detached"
                )
                entry["reply_payload"] = {
                    "body": build_body(r["body"], addressed_id, link_for(args, r))
                }
                if r.get("mode") in RESOLVE_MODES:
                    entry["resolve_thread_id"] = thread_ids.get(in_reply_to_id)
            else:
                entry["sentinel_patch"] = {
                    "finding_id": in_reply_to_id,
                    "sentinel": SENTINEL.format(id=addressed_id),
                }
            plan.append(entry)
        print(json.dumps({"plan": plan}, ensure_ascii=False))
        return 0

    text_total = fix + question  # body-bearing buckets post a text reply
    sentinel_total = ack  # only acknowledgment dedups via a parent-Finding PATCH
    resolve_total = sum(1 for r in replies if r.get("mode") in RESOLVE_MODES)
    text_ok = 0
    react_ok = 0
    patch_ok = 0
    resolve_ok = 0

    def do_resolve(in_reply_to_id: str) -> None:
        """Resolve the settled thread for a reply that landed (#75), best-effort.
        Shared by the batch and fallback paths; a missing thread id (degraded
        reviewThreads read) just skips, leaving the thread open."""
        nonlocal resolve_ok
        tid = thread_ids.get(in_reply_to_id)
        if not tid:
            print(f"no thread id for finding {in_reply_to_id}; skipping resolve", file=sys.stderr)
            return
        rrc, rerr = resolve_thread(tid)
        if rrc == 0:
            resolve_ok += 1
        else:
            print(f"resolveReviewThread failed for thread {tid}: {rerr.strip()}", file=sys.stderr)

    # Partition body-bearing replies: batch under one pending review when we have
    # a PR node id AND the reply's thread id (#38); otherwise fall back to a
    # detached REST reply — a degraded reviewThreads read (no pr node id) or a
    # thread past the first-100 page (no thread id). acknowledgments carry no body
    # and post no text reply, so they sit out the batch entirely.
    batched: list[dict] = []
    fallback: list[dict] = []
    for r in replies:
        if not r.get("body"):
            continue
        if args.pr_node_id and thread_ids.get(str(r["in_reply_to_id"])):
            batched.append(r)
        else:
            fallback.append(r)

    # --- Batch: one COMMENTED review (empty body) wraps every batchable reply, so
    # the operator gets a single notification for the tick (#38). A failed add or
    # submit leaves no sentinel on the affected thread, so the next cycle retries
    # it — same best-effort contract as the detached path.
    if batched:
        if args.existing_pending_review_id:
            drc, derr = delete_pending_review(args.existing_pending_review_id)
            if drc != 0:
                # Non-fatal here; the create below fails loudly if the stale
                # review really still blocks GitHub's one-pending-per-viewer rule.
                print(
                    f"could not delete stale pending review "
                    f"{args.existing_pending_review_id}: {derr.strip()}",
                    file=sys.stderr,
                )
        _crc, review_id, cerr = create_pending_review(args.pr_node_id)
        if not review_id:
            print(
                f"pending review create failed; {len(batched)} repl(y/ies) deferred "
                f"to next cycle: {cerr.strip()}",
                file=sys.stderr,
            )
        else:
            added: list[dict] = []
            for r in batched:
                tid = thread_ids[str(r["in_reply_to_id"])]
                full_body = build_body(r["body"], str(r["addressed_comment_id"]), link_for(args, r))
                arc, aerr = add_thread_reply(review_id, tid, full_body)
                if arc == 0:
                    added.append(r)
                else:
                    print(
                        f"thread reply add failed for thread {tid}: {aerr.strip()}", file=sys.stderr
                    )
            if not added:
                # A COMMENTED review with no comments and no body is rejected, so
                # discard the empty pending review rather than submit it.
                delete_pending_review(review_id)
                print(
                    "no thread replies added; discarded the empty pending review", file=sys.stderr
                )
            else:
                src, serr = submit_review(review_id)
                if src == 0:
                    text_ok += len(added)
                    # Resolve only after submit — replies (and their sentinels)
                    # are not live until the review is submitted.
                    for r in added:
                        if r.get("mode") in RESOLVE_MODES:
                            do_resolve(str(r["in_reply_to_id"]))
                else:
                    print(
                        f"review submit failed; {len(added)} repl(y/ies) deferred to "
                        f"next cycle: {serr.strip()}",
                        file=sys.stderr,
                    )

    # --- Fallback: a detached REST reply for any non-batchable body reply. Same
    # path the daemon used before #38; resolution stays inline since these post
    # immediately (no pending-review barrier).
    for r in fallback:
        in_reply_to_id = str(r["in_reply_to_id"])
        full_body = build_body(r["body"], str(r["addressed_comment_id"]), link_for(args, r))
        rc, err = post_reply(args.owner, args.repo, args.number, in_reply_to_id, full_body)
        if rc == 0:
            text_ok += 1
            if r.get("mode") in RESOLVE_MODES:
                do_resolve(in_reply_to_id)
        else:
            print(f"reply POST failed for comment {in_reply_to_id}: {err.strip()}", file=sys.stderr)

    # --- Reactions + bodiless sentinel PATCH: every reply, independent of the
    # text-reply path above. The reaction is idempotent; an acknowledgment then
    # embeds the Reply sentinel in its parent Finding once the reaction lands (its
    # only dedup carrier). A running copy of each Finding body lets two non-claim
    # replies on the same Finding accumulate both sentinels instead of clobbering.
    finding_work = dict(finding_bodies)
    for r in replies:
        bucket = r["bucket"]
        addressed_id = str(r["addressed_comment_id"])
        in_reply_to_id = str(r["in_reply_to_id"])
        has_body = bool(r.get("body"))

        content = BUCKET_REACTION[bucket]
        rc, err = post_reaction(args.owner, args.repo, addressed_id, content)
        if rc != 0:
            print(
                f"reaction POST failed for comment {addressed_id}: {err.strip()}",
                file=sys.stderr,
            )
            # The reaction is a bodiless thread's only ack, so skip the sentinel
            # PATCH and let the next cycle retry the reaction first.
            continue
        react_ok += 1

        if not has_body:
            base = finding_work.get(in_reply_to_id)
            if base is None:
                print(
                    f"no parent body for finding {in_reply_to_id}; "
                    f"skipping sentinel PATCH for reply {addressed_id}",
                    file=sys.stderr,
                )
                continue
            new_body = f"{base}\n\n{SENTINEL.format(id=addressed_id)}"
            rc, err = patch_finding(args.owner, args.repo, in_reply_to_id, new_body)
            if rc == 0:
                finding_work[in_reply_to_id] = new_body
                patch_ok += 1
            else:
                print(
                    f"sentinel PATCH failed for finding {in_reply_to_id}: {err.strip()}",
                    file=sys.stderr,
                )

    summary = f"done — {text_ok}/{text_total} replies, {react_ok}/{len(replies)} reactions"
    if sentinel_total:
        summary += f", {patch_ok}/{sentinel_total} sentinels"
    if resolve_total:
        summary += f", {resolve_ok}/{resolve_total} threads resolved"
    print(summary + " posted", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
