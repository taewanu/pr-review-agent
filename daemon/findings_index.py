"""Render the cumulative findings index for the Status comment (ADR 0020).

The index is the PR-level view of every daemon-authored Finding thread: a
`total · open · resolved` rollup plus one linked entry per thread, each carrying
the Finding's location, a link to its Inline comment, and its open/resolved state
read from the thread. It never carries the Finding body, which stays in the Inline
comment (one source per fact). It is a derived view, rebuilt each tick from the
live threads, so it holds no state of its own to fall out of sync.

Unanchored Findings (the Review body's `## Findings outside the diff` section,
ADR 0005) have no thread and so no resolvable state; the index notes the current
review's count as a pointer to the Review, never a tracked per-item entry
(ADR 0020 Decision 4).

Input is the thread array `fetch_open_review_threads` (lib.sh) emits, both open
and resolved. Output is the markdown block `render_status_comment` inserts: the
findings index, or a `No findings` affirmation when the review surfaced nothing
(ADR 0020 Decision 6).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NamedTuple

# Provenance marker on every daemon-authored comment (ADR 0010 §3). Mirrors
# lib.sh's PROVENANCE_TAG and resolve_threads.py's PROVENANCE_MARKER;
# test_provenance_tag.py pins them identical. Tells a daemon Finding thread from
# the Operator's own manual review comment, so the index counts only daemon Findings.
PROVENANCE_MARKER = "🤖 _pr-review-agent_"


def daemon_findings(threads: list[dict], operator: str) -> list[dict]:
    """The PR's daemon-authored Finding threads, open and resolved alike.

    Unlike resolve_threads.select, this keeps resolved threads (the index reports
    them as resolved) and stamped ones (a stamp is the resolved state, not a skip)."""
    return [
        t
        for t in threads
        if t.get("root_author") == operator and PROVENANCE_MARKER in (t.get("root_body") or "")
    ]


def _order(findings: list[dict]) -> list[dict]:
    """Open Findings first (the actionable ones), then by path and line. Resolved
    sink to the bottom as a settled record."""
    return sorted(
        findings,
        key=lambda t: (
            bool(t.get("is_resolved")),
            t.get("path") or "",
            t.get("original_line") if isinstance(t.get("original_line"), int) else 0,
        ),
    )


def _location(thread: dict) -> str:
    """The `path:line` label for a Finding, or just `path` when the line is gone
    (a thread nulls its line once outdated)."""
    path = thread.get("path") or "?"
    line = thread.get("original_line")
    return f"{path}:{line}" if isinstance(line, int) else path


def _entry(thread: dict) -> str:
    """Render one Finding thread as its index line: a markdown list item carrying
    the Finding's location linked to its Inline comment, then its open/resolved state.

    Available on `thread`: `_location(thread)` gives the `path:line` label;
    `thread["root_comment_url"]` is the Inline comment's URL (may be None — fall back
    to no link); `thread["is_resolved"]` is the open/resolved bool. Keep it a pointer:
    location and state only, never the Finding body (ADR 0020). Wrap the label in
    backticks. Example shape: "- [`daemon/foo.py:16`](https://…) · resolved"
    """
    label = _location(thread)
    url = thread.get("root_comment_url")
    state = "resolved" if thread.get("is_resolved") else "open"
    linked = f"[`{label}`]({url})" if url else f"`{label}`"
    return f"- {linked} · {state}"


def _clean_affirmation(summary: str | None) -> str:
    """The clean-review affirmation: a `No findings` rollup, plus the review's
    summary as a blockquote when one is present, for a tick that surfaced nothing.

    Without it a zero-finding review leaves no verdict, since #166 suppresses the
    Review object. Why a summary belongs here and cannot drift: ADR 0020 Decision 6.
    """
    lines = ["**No findings**"]
    text = (summary or "").strip()
    if text:
        lines.append("")
        lines.extend(f"> {ln}" if ln else ">" for ln in text.splitlines())
    return "\n".join(lines)


class Delta(NamedTuple):
    """This tick's per-push counts (ADR 0033): `new` findings posted, `fixed`
    threads the commit-driven resolution stamped resolved. The two always travel
    together (both set on a re-review, neither on a first review), so they ride as
    one value rather than a pair of parallel params."""

    new: int
    fixed: int


def _delta_line(delta: Delta) -> str:
    """Render the per-push delta as italic index chrome above the rollup (ADR 0033
    Decision 4), or `no change` when the push moved nothing (why the both-zero case
    affirms rather than blanks: ADR 0033 Decision 4, ADR 0020 Decision 6)."""
    parts = []
    if delta.new > 0:
        parts.append(f"+{delta.new} new")
    if delta.fixed > 0:
        parts.append(f"{delta.fixed} fixed")
    if not parts:
        return "_no change_"
    return f"_{' · '.join(parts)}_"


def render_index(
    threads: list[dict],
    operator: str,
    unanchored_count: int = 0,
    review_url: str | None = None,
    summary: str | None = None,
    delta: Delta | None = None,
) -> str:
    """The full index block, or the clean-review affirmation (`No findings` +
    optional summary) when the PR has no Findings (ADR 0020 Decision 6).

    `delta` carries this tick's per-push counts (ADR 0033): None on a first review
    (no prior push to compare) suppresses the line; a re-review passes a `Delta`
    and `_delta_line` renders it above the rollup."""
    findings = daemon_findings(threads, operator)
    total = len(findings)
    if total == 0 and unanchored_count <= 0:
        return _clean_affirmation(summary)

    delta_line = _delta_line(delta) if delta is not None else ""

    lines: list[str] = []
    if delta_line:
        lines.append(delta_line)
        lines.append("")
    if total > 0:
        resolved = sum(1 for t in findings if t.get("is_resolved"))
        open_count = total - resolved
        noun = "finding" if total == 1 else "findings"
        lines.append(f"**{total} {noun} · {open_count} open · {resolved} resolved**")
        lines.append("")
        lines.extend(_entry(t) for t in _order(findings))

    if unanchored_count > 0:
        pointer = f"+ {unanchored_count} outside the diff"
        if review_url:
            pointer += f" → [review]({review_url})"
        if lines:
            lines.append("")
        lines.append(f"_{pointer}_")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render the Status comment findings index.")
    ap.add_argument("--threads", required=True, help="JSON file of review threads")
    ap.add_argument("--operator", required=True)
    ap.add_argument("--unanchored", type=int, default=0, help="this review's outside-diff count")
    ap.add_argument("--review-url", default="", help="link target for the outside-diff pointer")
    ap.add_argument(
        "--summary-file", default="", help="review summary, quoted in the clean affirmation"
    )
    # Per-push delta (ADR 0033): both set on a re-review, both absent on a first
    # review so the delta line is suppressed (no prior push to compare against).
    ap.add_argument("--new", type=int, default=None, help="findings posted this tick")
    ap.add_argument("--fixed", type=int, default=None, help="threads resolved this tick")
    args = ap.parse_args()

    # Best-effort: an unreadable or malformed thread file yields an empty index
    # rather than aborting the review that has already landed.
    try:
        threads = json.loads(Path(args.threads).read_text() or "[]")
    except (OSError, ValueError):
        threads = []

    # Same best-effort read: a missing summary degrades to the bare rollup, never
    # an abort. Only consulted on the zero-finding path.
    summary = ""
    if args.summary_file:
        try:
            summary = Path(args.summary_file).read_text()
        except OSError:
            summary = ""

    # Both counts present → a re-review's delta; either absent → a first review,
    # so no delta line (ADR 0033 Decision 3).
    delta = Delta(args.new, args.fixed) if args.new is not None and args.fixed is not None else None
    block = render_index(
        threads,
        args.operator,
        args.unanchored,
        args.review_url or None,
        summary or None,
        delta,
    )
    sys.stdout.write(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
