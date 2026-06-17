"""Shared resolution toolkit for the two thread-resolution drivers (#159).

Both drivers record a resolution the same way (ADR 0019): they edit a Resolution
stamp into the resolved Finding's own comment and resolve its GitHub review thread.
commit-driven (resolve_threads.py) and reply-driven (create_reply.py) call these
leaves so neither orchestrator imports the other:

  build_stamp / append_stamp: the one-line stamp text and its idempotent append.
  update_review_comment: overwrite the Finding comment's body with the stamp.
  resolve_thread: flip the GitHub review thread to resolved.

The blob-at-HEAD permalink a stamp may embed is built in links.py and passed in as
a ready string. `input=""` on every `gh` call closes stdin, since the query rides
in argv.
"""

from __future__ import annotations

import subprocess

# Single-line so the test gh-stub records the call on one argv line. GraphQL is
# whitespace-insensitive, so this is equivalent to the pretty form.
RESOLVE_MUTATION = (
    "mutation($threadId: ID!) { resolveReviewThread(input: {threadId: $threadId}) "
    "{ thread { id isResolved } } }"
)
# Replaces a review comment's whole body (#159, ADR 0019): the resolution stamp is
# appended to the Finding's root comment in place, so the caller sends the existing
# body plus the stamp. `updatePullRequestReviewComment` overwrites body wholesale,
# which is why append-vs-overwrite is the caller's concern (append_stamp).
UPDATE_COMMENT_MUTATION = (
    "mutation UpdateComment($commentId: ID!, $body: String!) { "
    "updatePullRequestReviewComment(input: {pullRequestReviewCommentId: $commentId, body: $body}) "
    "{ pullRequestReviewComment { id } } }"
)

# Hidden dedup marker on a Resolution stamp (ADR 0019, #159). The stamp is edited
# into the Finding's own root comment, so this marker lives there, not on a separate
# note. Both resolution drivers (commit-driven resolve_threads, reply-driven
# create_reply) build the stamp here so neither imports the other; lib.sh's
# fetch_open_review_threads scans the root comment for this same literal to compute
# has_resolution_stamp, and test_resolve_threads pins the two identical.
RESOLUTION_SENTINEL = "<!-- pr-review-agent:resolved -->"


def build_stamp(rationale: str, link: str | None) -> str:
    """Assemble the Resolution stamp appended to a resolved Finding's own comment
    (ADR 0019, #159): one visible line carrying a lead, an optional commit-anchored
    blob link (built in links.py), and the one-line rationale, then the hidden
    RESOLUTION_SENTINEL.

    The stamp is appended to the Finding's root comment, which already carries the
    Provenance marker, so the stamp adds none. `link` is None when there is nothing
    to anchor (the reply-driven path, or no head sha/line); the lead then drops its
    "in" clause. Voice-gating runs on the rationale before this is called, not here."""
    lead = f"✅ _Resolved in_ {link}" if link else "✅ _Resolved_"
    return "\n\n".join([f"{lead}: {rationale}", RESOLUTION_SENTINEL])


def append_stamp(body: str, stamp: str) -> str | None:
    """Return the Finding comment `body` with the Resolution stamp appended below it,
    or None when `body` already carries a stamp (#159, ADR 0019).

    Idempotence guard for the in-place edit. Both drivers exclude already-stamped
    threads before reaching here (commit-driven via select_candidates, reply-driven
    via reply dedup), so this is a re-run backstop: it makes a second pass over the
    same comment a no-op rather than a double-stamp. A None return means "already
    stamped: skip the edit, but still resolve the thread"; a string return is the new
    comment body (existing body plus the stamp) for update_review_comment. The stamp's
    own dedup marker is RESOLUTION_SENTINEL."""
    if RESOLUTION_SENTINEL in body:
        return None
    return "\n\n".join([body, stamp])


def _graphql(query: str, **variables: str) -> subprocess.CompletedProcess:
    """Run one `gh api graphql` mutation. Variables go through `-f name=value`, so
    gh JSON-encodes each value — a body with `\\n` / backticks / non-ASCII survives
    byte-for-byte without any shell quoting (same guarantee as the REST `--input -`
    path). `input=""` closes stdin since the query rides in argv."""
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        args += ["-f", f"{name}={value}"]
    return subprocess.run(args, input="", capture_output=True, text=True, check=False)


def resolve_thread(thread_id: str) -> tuple[int, str]:
    """Resolve a GitHub review thread via the GraphQL resolveReviewThread mutation
    (#75). Idempotent on GitHub's side — safe on an already-resolved thread.
    Best-effort: callers log a failure and move on, never retrying."""
    proc = _graphql(RESOLVE_MUTATION, threadId=thread_id)
    return proc.returncode, proc.stderr


def update_review_comment(comment_id: str, body: str) -> tuple[int, str]:
    """Overwrite a review comment's body via updatePullRequestReviewComment (#159).
    The caller passes the existing comment plus the resolution stamp as `body` (see
    UPDATE_COMMENT_MUTATION). Best-effort: the caller logs a failure and leaves the
    thread open (safe bias, ADR 0017)."""
    proc = _graphql(UPDATE_COMMENT_MUTATION, commentId=comment_id, body=body)
    return proc.returncode, proc.stderr
