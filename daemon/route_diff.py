"""Classify a diff as prose-only or behaviour-bearing (#219).

Observation only for now: `review-pr.sh` logs the verdict and changes nothing.
Whether routing the `code` role away from prose-only diffs is worth building is
undecided, and the log against real PRs is what decides it.

Written in Python rather than in `lib.sh` so it reads paths through
`diff_paths.parse_diff_path`, the one parser that handles a quoted or
space-bearing path. Reading the `diff --git a/OLD b/NEW` header instead, as a
shell one-liner would, drops a non-ASCII path silently, and a dropped path here
reads as prose that is not there.

Safe-biased throughout: anything unclassified is behaviour, because a wrong
"prose" answer is the one that would let a review be skipped.

Usage: `python3 route_diff.py <unified-diff-file>` exits 0 when the diff carries
behaviour, 1 when every changed path is prose.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from diff_paths import parse_diff_path  # noqa: E402

# An allowlist of prose, not a denylist of code: a file type nobody classified
# has to fall through to behaviour so it still reaches the code role. `.txt` is
# deliberately absent, since `requirements.txt` and `CMakeLists.txt` wear it;
# so is `.lock`, since a dependency bump changes what runs.
PROSE_SUFFIXES = (".md", ".rst", ".adoc")

# Agent definitions and project instructions are review behaviour written in
# markdown, so the suffix does not decide for them. Matched on any path segment,
# because a bundled checkout can carry them below the root.
BEHAVIOUR_NAMES = ("CLAUDE.md", "AGENTS.md")
BEHAVIOUR_DIRS = (".claude",)


def path_is_prose(path: str) -> bool:
    """True when the path carries no behaviour."""
    parts = Path(path).parts
    if any(part in BEHAVIOUR_DIRS for part in parts):
        return False
    if parts and parts[-1] in BEHAVIOUR_NAMES:
        return False
    return path.endswith(PROSE_SUFFIXES)


def diff_paths(text: str) -> list[str]:
    """Every path the diff touches, deduped, in first-seen order.

    Reads both header sides so a delete (whose `+++` is `/dev/null`) still
    yields its path.
    """
    seen: dict[str, None] = {}
    for line in text.splitlines():
        for marker in ("a/", "b/"):
            path = parse_diff_path(line, marker)
            if path is not None:
                seen.setdefault(path, None)
    return list(seen)


def has_executable_change(text: str) -> bool:
    """True unless every path the diff touches is prose.

    An empty or unparseable diff is behaviour: nothing to be confident about
    means nothing to skip on.
    """
    paths = diff_paths(text)
    if not paths:
        return True
    return not all(path_is_prose(path) for path in paths)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: route_diff.py <unified-diff-file>", file=sys.stderr)
        return 2
    try:
        text = Path(argv[0]).read_text(errors="replace")
    except OSError:
        return 0  # unreadable diff: behaviour, like every other unknown
    return 0 if has_executable_change(text) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
