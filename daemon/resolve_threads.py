#!/usr/bin/env python3
"""Commit-driven thread resolution: candidate selection + verdict parsing (#125, ADR 0017).

The review path runs this on each new HEAD SHA to find prior Findings a commit
may have fixed without any Operator reply. Slice A is dry-run: this module picks
the candidate threads and parses the Fix-check agent's verdict, but writes
nothing. The `_Fixed:_` note and the resolve land in later slices.

Two pure entry points, both safe-biased toward leaving a thread open (ADR 0017):

  select_candidates: an open, daemon-owned thread whose Finding line was touched
    by this increment's diff. The line tested is the thread's `originalLine` (its
    coordinate in the commit the Finding was posted against), matched against the
    OLD side of `git diff LAST_SHA..HEAD`. GitHub's GraphQL `line` is null exactly
    when a thread goes outdated, which is precisely when its code changed (the case
    we must catch), so `line` is unusable here and `originalLine` is the only
    surviving coordinate. An open Finding's code is untouched since it was posted
    (a prior increment would have judged it otherwise), so `originalLine` stays a
    valid old-side coordinate across multiple ticks.

  parse_verdict: the Fix-check agent's {fixed, rationale}. Any parse or schema
    failure returns fixed=False, so a malformed judgment leaves the thread open.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<old>.+) b/(?P<new>.+)$")
# Old-side range of a hunk: `@@ -<start>,<count> +... @@`. Count defaults to 1
# when omitted; a count of 0 is a pure insertion with no old-side lines.
HUNK_OLD_RE = re.compile(r"^@@ -(?P<start>\d+)(?:,(?P<count>\d+))? \+\d+(?:,\d+)? @@")

# Provenance marker on every daemon-authored comment (ADR 0010 §3). Mirrors
# lib.sh's PROVENANCE_TAG and create_reply.py's MARKER; test_provenance_tag.py
# pins all three identical. Used here to tell a daemon Finding thread from the
# Operator's own manual review comment, so only the daemon's own threads can
# become resolution candidates.
PROVENANCE_MARKER = "🤖 _pr-review-agent_"


@dataclass
class IncrementDiff:
    """Old-side hunk ranges of an increment diff, keyed by file path.

    Distinct from anchor_findings.Diff, which parses the NEW side to anchor fresh
    Findings; here a Finding from a prior review is matched against the OLD side
    to ask whether this increment touched the line it was posted on.
    """

    ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "IncrementDiff":
        d = cls()
        paths: list[str] = []
        for line in text.splitlines():
            m = DIFF_GIT_RE.match(line)
            if m:
                # Register under both old and new path so a thread anchored to
                # either name (renames) still matches; they coincide otherwise.
                paths = [m.group("old"), m.group("new")]
                for p in paths:
                    d.ranges.setdefault(p, [])
                continue
            if not paths:
                continue
            m = HUNK_OLD_RE.match(line)
            if m:
                start = int(m.group("start"))
                count = int(m.group("count")) if m.group("count") else 1
                if count == 0:
                    continue
                for p in paths:
                    d.ranges[p].append((start, start + count - 1))
        return d

    def touched(self, path: str, line: int, start_line: int | None = None) -> bool:
        """Whether [start_line or line .. line] overlaps an old-side hunk for path."""
        if path not in self.ranges or line <= 0:
            return False
        lo = start_line if (start_line and start_line > 0) else line
        hi = line
        if lo > hi:
            lo, hi = hi, lo
        return any(s <= hi and lo <= e for s, e in self.ranges[path])


def select_candidates(
    threads: list[dict], diff: IncrementDiff, operator: str, all_open: bool = False
) -> list[dict]:
    """Open, daemon-owned threads whose Finding line this increment touched.

    A candidate is the thread of a prior Finding the new HEAD might have fixed:
      - open (not already resolved on GitHub),
      - daemon-owned (root comment authored by the operator AND carrying the
        Provenance marker, so an Operator's own manual comment never qualifies),
      - touched: its `original_line` (creation-side coordinate) falls inside an
        old-side hunk of this increment's diff.

    `all_open` drops the touched test and takes every open, daemon-owned thread.
    The review path sets it on a force-push or rebase, where the increment diff
    can't be computed and the Findings' creation-side lines no longer share a
    coordinate space with the full PR diff (ADR 0017 §1's "full PR diff" scope).
    """
    candidates: list[dict] = []
    for t in threads:
        if t.get("is_resolved"):
            continue
        if t.get("root_author") != operator:
            continue
        body = t.get("root_body") or ""
        if PROVENANCE_MARKER not in body:
            continue
        path = t.get("path") or ""
        line = t.get("original_line")
        if not isinstance(line, int):
            continue
        start_line = t.get("original_start_line")
        if not isinstance(start_line, int):
            start_line = None
        if not all_open and not diff.touched(path, line, start_line):
            continue
        candidates.append(
            {
                "thread_id": t["thread_id"],
                "path": path,
                "line": line,
                "finding_body": body,
            }
        )
    return candidates


def parse_verdict(raw: str) -> dict:
    """Parse the Fix-check agent's stdout into {fixed: bool, rationale: str}.

    Same last-```json-fence convention as the review and reply agents. Safe-biased
    (ADR 0017): any parse failure or missing/invalid field returns fixed=False, so
    a malformed judgment leaves the thread open rather than resolving it.
    """
    matches = re.findall(r"```json\s*\n(.*?)\n```", raw, re.DOTALL)
    if not matches:
        return {"fixed": False, "rationale": "no json fence in fix-check output"}
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        return {"fixed": False, "rationale": f"verdict parse failed: {exc}"}
    fixed = data.get("fixed")
    rationale = data.get("rationale")
    if not isinstance(fixed, bool) or not isinstance(rationale, str) or not rationale.strip():
        return {"fixed": False, "rationale": "verdict missing 'fixed' bool or 'rationale' text"}
    return {"fixed": fixed, "rationale": rationale.strip()}


def _cmd_select(args: argparse.Namespace) -> int:
    threads = json.loads(args.threads.read_text())
    diff = IncrementDiff.parse(args.diff.read_text())
    candidates = select_candidates(threads, diff, args.operator, all_open=args.all_open)
    print(json.dumps(candidates, ensure_ascii=False))
    return 0


def _cmd_parse_verdict(args: argparse.Namespace) -> int:
    verdict = parse_verdict(args.raw.read_text())
    print(json.dumps(verdict, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_select = sub.add_parser("select", help="emit candidate threads as JSON")
    p_select.add_argument("--threads", type=Path, required=True, help="open-threads JSON array")
    p_select.add_argument(
        "--diff", type=Path, required=True, help="increment diff (LAST_SHA..HEAD)"
    )
    p_select.add_argument("--operator", required=True, help="daemon's gh login")
    p_select.add_argument(
        "--all-open",
        action="store_true",
        help="take every open daemon thread, skipping the diff filter (force-push/rebase)",
    )
    p_select.set_defaults(func=_cmd_select)

    p_verdict = sub.add_parser("parse-verdict", help="emit {fixed, rationale} from agent output")
    p_verdict.add_argument("raw", type=Path, help="Fix-check agent stdout capture")
    p_verdict.set_defaults(func=_cmd_parse_verdict)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
