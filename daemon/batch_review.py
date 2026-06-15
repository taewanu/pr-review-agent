"""Shared batched-review posting toolkit (#38, #125).

Both posting paths wrap a tick's threaded comments in ONE pending COMMENT review
and submit it once, so the Operator gets a single GitHub notification instead of
one per comment. create_reply.py wraps reply acks; resolve_threads.py wraps
`_Fixed:_` notes. The create -> add -> submit -> delete ladder was identical in
both and drifted independently; `submit` is that ladder extracted once, leaving
each caller only its own resolve policy (the two resolve different thread sets).

The leaf GraphQL helpers live here too, so neither orchestrator imports the
other for them. `build_blob_link` (the comment-body permalink helper both paths
use) rides along: it builds the links embedded in the comments this module
posts.

`input=""` on every `gh` call closes stdin, since the query rides in argv.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

# Hidden marker on the wrapper review body (renders empty). It is the
# discriminator reply-pr.sh / review-pr.sh filter on when discarding a stale
# wrapper, so the daemon only ever deletes its OWN comment wrappers — never a
# Finding-bearing Pending review left pending as the ADR 0008 safety gate, nor a
# human's manual draft. Two producers emit it (create_reply reply acks,
# resolve_threads fix-notes), so renaming the wire string must keep both in sync;
# lib.sh's find_stale_wrapper_review and reply-pr.sh pin this same literal.
WRAPPER_MARKER = "<!-- pr-review-agent:reply-review -->"

# Each mutation is named so the test gh-stub can match the call by operation
# name. CreatePendingReview's response carries the new review id the adds and
# submit need; addPullRequestReviewThreadReply attaches a reply to the pending
# review AND to its parent thread, preserving threading.
CREATE_REVIEW_MUTATION = (
    "mutation CreatePendingReview($pr: ID!, $body: String!) { "
    "addPullRequestReview(input: {pullRequestId: $pr, body: $body}) "
    "{ pullRequestReview { id } } }"
)
ADD_REPLY_MUTATION = (
    "mutation AddReply($review: ID!, $thread: ID!, $body: String!) { "
    "addPullRequestReviewThreadReply(input: {pullRequestReviewId: $review, "
    "pullRequestReviewThreadId: $thread, body: $body}) { comment { id } } }"
)
# Submit as COMMENT, setting the review body to the disposition summary plus the
# hidden WRAPPER_MARKER (#11). submitPullRequestReview accepts a `body` that
# overrides the create-time body.
SUBMIT_REVIEW_MUTATION = (
    "mutation SubmitReview($review: ID!, $body: String!) { "
    "submitPullRequestReview(input: {pullRequestReviewId: $review, event: COMMENT, body: $body}) "
    "{ pullRequestReview { id state } } }"
)
# Discards a stale pending review (a prior tick that added comments but failed to
# submit). GitHub allows one pending review per viewer per PR, so a leftover would
# block this tick's create; deleting it also drops its orphan comments, which are
# invisible to the REST scan and would otherwise re-trigger duplicate posts.
DELETE_REVIEW_MUTATION = (
    "mutation DeletePendingReview($review: ID!) { "
    "deletePullRequestReview(input: {pullRequestReviewId: $review}) "
    "{ pullRequestReview { id } } }"
)
# Single-line so the test gh-stub records the call on one argv line. GraphQL is
# whitespace-insensitive, so this is equivalent to the pretty form.
RESOLVE_MUTATION = (
    "mutation($threadId: ID!) { resolveReviewThread(input: {threadId: $threadId}) "
    "{ thread { id isResolved } } }"
)


def build_blob_link(
    owner: str,
    repo: str,
    head_sha: str,
    path: str | None,
    line: object,
    end_line: object = None,
) -> str | None:
    """A GitHub blob-at-HEAD permalink to the verified line(s), or None when there
    is nothing to anchor (no head sha, or the caller emitted no line, for example a
    confirmed-by-deletion).

    `owner`/`repo` must be the **head** repo: `head_sha` is a commit in the fork,
    so a base-repo blob URL 404s on a cross-repo PR. The link asserts the file's
    *current* state, so it points at the blob at HEAD (#11), not a per-commit diff.
    The anchor is GitHub's plain `#L<n>` blob form; the sha256 path hash is the
    per-commit *diff* anchor and does not apply here. The label shows a short sha
    plus line so the destination reads without hovering; the URL carries the full
    sha for stability (a permalink)."""
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


def _graphql(query: str, **variables: str) -> subprocess.CompletedProcess:
    """Run one `gh api graphql` mutation. Variables go through `-f name=value`, so
    gh JSON-encodes each value — a body with `\\n` / backticks / non-ASCII survives
    byte-for-byte without any shell quoting (same guarantee as the REST `--input -`
    path). `input=""` closes stdin since the query rides in argv."""
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        args += ["-f", f"{name}={value}"]
    return subprocess.run(args, input="", capture_output=True, text=True, check=False)


def create_pending_review(pr_node_id: str) -> tuple[int, str | None, str]:
    """Open a pending COMMENTED review on the PR (#38). Returns
    (returncode, review_node_id, stderr); review_node_id is None when the call
    failed or the response did not carry an id, so the caller defers the batch to
    the next cycle rather than posting into a review that never opened.

    The review body is the hidden WRAPPER_MARKER (renders empty), tagging the
    wrapper so a stale one can be told apart from a Finding-bearing draft before
    deletion."""
    proc = _graphql(CREATE_REVIEW_MUTATION, pr=pr_node_id, body=WRAPPER_MARKER)
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


def submit_review(review_id: str, body: str) -> tuple[int, str]:
    """Submit the pending review as COMMENT, the tick's single notification. The
    body carries the caller's summary, the Provenance tag, then the hidden marker
    (#11; tag added #132)."""
    proc = _graphql(SUBMIT_REVIEW_MUTATION, review=review_id, body=body)
    return proc.returncode, proc.stderr


def delete_pending_review(review_id: str) -> tuple[int, str]:
    """Discard a pending review (a stale one before creating, or this tick's own
    review when nothing was added so we never submit an empty COMMENTED review)."""
    proc = _graphql(DELETE_REVIEW_MUTATION, review=review_id)
    return proc.returncode, proc.stderr


def resolve_thread(thread_id: str) -> tuple[int, str]:
    """Resolve a GitHub review thread via the GraphQL resolveReviewThread mutation
    (#75). Idempotent on GitHub's side — safe on an already-resolved thread.
    Best-effort: callers log a failure and move on, never retrying."""
    proc = _graphql(RESOLVE_MUTATION, threadId=thread_id)
    return proc.returncode, proc.stderr


@dataclass
class BatchItem:
    """One threaded comment to wrap in the batched review.

    `tag` is opaque caller data returned untouched on the landed items, so each
    caller can run its own resolve policy over what actually posted (the reply
    path reads the reply dict; the fix path needs only `thread_id`)."""

    thread_id: str
    body: str
    tag: object = None


def submit(
    items: list[BatchItem],
    *,
    pr_node_id: str,
    review_body: Callable[[list[BatchItem]], str],
    existing_review_id: str = "",
) -> list[BatchItem]:
    """Post `items` as one batched COMMENT review and return the items that landed.

    The ladder, best-effort throughout: discard any stale wrapper, open a pending
    review, add each comment, submit as COMMENT with `review_body(added)`. Returns
    the added items on success, or `[]` on any failure (create, all-adds-fail,
    submit) so the caller leaves those threads untouched and a later tick retries.
    `review_body` receives the added items, so a caller can count them or read
    their composition (the reply path splits open vs resolved; the fix path uses
    `len`).

    Resolution is the caller's job: the two paths resolve different thread sets,
    and `resolve_thread` is already idempotent, so there is no shared ladder left
    in it to drift."""
    if not items:
        return []
    if existing_review_id:
        drc, derr = delete_pending_review(existing_review_id)
        if drc != 0:
            # Non-fatal: the create below fails loudly if the stale review really
            # still blocks GitHub's one-pending-per-viewer rule.
            print(
                f"could not delete stale pending review {existing_review_id}: {derr.strip()}",
                file=sys.stderr,
            )
    if not pr_node_id:
        print(
            f"no PR node id; cannot batch {len(items)} comment(s), leaving threads open",
            file=sys.stderr,
        )
        return []
    _crc, review_id, cerr = create_pending_review(pr_node_id)
    if not review_id:
        print(
            f"pending review create failed; {len(items)} comment(s) deferred to next cycle: "
            f"{cerr.strip()}",
            file=sys.stderr,
        )
        return []
    added: list[BatchItem] = []
    for it in items:
        arc, aerr = add_thread_reply(review_id, it.thread_id, it.body)
        if arc == 0:
            added.append(it)
        else:
            print(
                f"thread reply add failed for thread {it.thread_id}: {aerr.strip()}",
                file=sys.stderr,
            )
    if not added:
        # A COMMENTED review with no comments and no body is rejected, so discard
        # the empty pending review rather than submit it.
        delete_pending_review(review_id)
        print("no thread replies added; discarded the empty pending review", file=sys.stderr)
        return []
    src, serr = submit_review(review_id, review_body(added))
    if src != 0:
        delete_pending_review(review_id)
        print(
            f"review submit failed; {len(added)} comment(s) deferred to next cycle: {serr.strip()}",
            file=sys.stderr,
        )
        return []
    return added
