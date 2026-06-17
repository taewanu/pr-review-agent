"""GitHub link builders shared across the daemon's Python posters.

A small, cohesive home for "turn a (repo, sha, path, line) into a GitHub URL"
helpers. `build_blob_link` is the only resident today; it is embedded by both the
reply ack body (create_reply.py) and the commit-driven Resolution stamp
(resolve_threads.py), neither of which is "about" links, so the formatter lives
here rather than in either caller.

Backlog: create-review.sh:195 (jq) builds the same blob-at-HEAD URL for the
findings review. If the findings path ever moves link-building to Python, that
duplicate consolidates here.
"""

from __future__ import annotations


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
