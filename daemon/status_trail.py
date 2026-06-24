"""Render the reviewed-SHAs trail for the Status comment (B1, ADR 0021).

The trail is a folded `<details>` block listing every HEAD SHA the daemon has
reviewed on this PR, newest last, each with the UTC time it was reviewed. It is
the one part of the Status comment that ACCUMULATES across ticks rather than
being derived from current PR state. Contrast the findings index (ADR 0020),
rebuilt from live threads each tick and owning nothing; the trail owns its rows,
so they have nowhere to live but the Status comment body itself. Each reviewed
tick therefore parses the prior rows back out of the existing body, folds in the
current SHA, and re-emits the whole block.

Input is the existing Status comment body (the prior block embedded in it, or
nothing on a first review), read from --body or stdin. Output is the `<details>`
block render_status_comment inserts above the provenance line, or "" when the PR
has no reviewed SHAs yet.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# The trail block is fenced by this summary so parsing reads only its own rows,
# never a findings-index list item (ADR 0020) that resembles an entry. The inner
# group is the block body; _ENTRY_RE then pulls the rows from inside it.
_BLOCK_RE = re.compile(
    r"<details><summary>Reviewed \d+ commits?</summary>(.*?)</details>",
    re.DOTALL,
)
# A row is `- ` + a backtick-wrapped short SHA + ` · ` + the reviewed-at text.
_ENTRY_RE = re.compile(r"^- `([0-9a-f]{4,40})` · (.+)$", re.MULTILINE)


def parse_entries(body: str) -> list[tuple[str, str]]:
    """The (sha, time) rows already recorded in the body, in order. Empty when the
    body carries no trail block yet (a first review, or a pre-0021 comment)."""
    m = _BLOCK_RE.search(body or "")
    if not m:
        return []
    return [(sha, rest.strip()) for sha, rest in _ENTRY_RE.findall(m.group(1))]


def merge(
    entries: list[tuple[str, str]],
    add_sha: str | None = None,
    add_time: str | None = None,
) -> list[tuple[str, str]]:
    """Fold this tick's SHA into the prior entries.

    `entries` is the prior rows as (sha, time) in chronological order, oldest
    first. When `add_sha` is given (the terminal "Reviewed" render; it is absent
    on the pre-review "Reviewing…" render), return the entries with this tick
    folded in; when it is None, return them unchanged.

    Idempotency policy: skip a SHA already present. The trail answers one audit
    question, "was every commit on this PR reviewed?", so it is a set of distinct
    reviewed SHAs. A re-seen SHA is a retry after a crash or a re-review of an
    unchanged HEAD (same-SHA ticks are already skipped upstream at the poll); it
    is an implementation artifact, not an audit event, so it neither adds a row
    nor rewrites the first-reviewed timestamp. Pure: returns a new list.
    """
    if add_sha is None:
        return list(entries)
    if any(sha == add_sha for sha, _ in entries):
        return list(entries)
    return [*entries, (add_sha, add_time or "")]


def render(body: str, add_sha: str | None = None, add_time: str | None = None) -> str:
    """The full trail block, or "" when there is nothing to show."""
    entries = merge(parse_entries(body), add_sha, add_time)
    if not entries:
        return ""
    noun = "commit" if len(entries) == 1 else "commits"
    lines = [f"<details><summary>Reviewed {len(entries)} {noun}</summary>", ""]
    lines += [f"- `{sha}` · {when}" for sha, when in entries]
    lines += ["", "</details>"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render the Status comment reviewed-SHAs trail.")
    ap.add_argument("--body", help="file holding the existing comment body; stdin if omitted")
    ap.add_argument(
        "--add-sha", default=None, help="this tick's short HEAD SHA (terminal render only)"
    )
    ap.add_argument("--add-time", default=None, help="this tick's reviewed-at timestamp")
    args = ap.parse_args()

    body = Path(args.body).read_text() if args.body else sys.stdin.read()
    sys.stdout.write(render(body, args.add_sha, args.add_time or None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
