#!/usr/bin/env python3
"""Split a review payload into anchored (in-diff) and unanchored findings.

Parses `gh pr diff` output for the new-file hunks (GitHub's PR Review API
anchors comments to new-file lines, so old-file ranges aren't used). A finding
is anchored when its `path` is in the diff AND its `line` (and optional
`end_line`) falls inside a hunk on the new side. Range findings must have both
endpoints in the same hunk — GitHub rejects cross-hunk ranges with 422.

Per ADR 0005, findings whose (severity, type) combo is forbidden are dropped
before splitting and the count is emitted to stdout so the orchestrator can
note the degradation in the posted review body.
"""

import argparse
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<old>.+) b/(?P<new>.+)$")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")

# Combos reserved for surfacing system issues out-of-band — never posted as
# review comments. Per ADR 0002 + ADR 0005.
FORBIDDEN_COMBOS: frozenset[tuple[str, str]] = frozenset({("important", "polish")})


@dataclass
class _DiffLine:
    raw: str
    new_lineno: int | None = None  # new-file line number for an added/context line
    new_path: str | None = None  # new path, set on a `diff --git` header
    hunk: tuple[int, int] | None = None  # (start, count), set on a `@@` header


def _iter_diff_lines(text: str) -> Iterator[_DiffLine]:
    """Walk a unified diff, tagging each new-side line with its new-file number.

    The single home for the new-side line-numbering state machine, shared by
    `Diff.parse` (content anchoring) and `numbered_diff` (the agent's
    line-numbered input). Added and context lines carry a `new_lineno`; deleted
    lines and all headers do not, so a `+++` header is never read as an added
    line."""
    current_path: str | None = None
    new_lineno: int | None = None
    for line in text.splitlines():
        m = DIFF_GIT_RE.match(line)
        if m:
            current_path = m.group("new")
            new_lineno = None
            yield _DiffLine(line, new_path=current_path)
            continue
        if current_path is None:
            yield _DiffLine(line)
            continue
        m = HUNK_RE.match(line)
        if m:
            start = int(m.group("start"))
            count = int(m.group("count")) if m.group("count") else 1
            new_lineno = start
            yield _DiffLine(line, hunk=(start, count))
            continue
        if new_lineno is not None and line.startswith(("+", " ")):
            yield _DiffLine(line, new_lineno=new_lineno)
            new_lineno += 1
        else:
            yield _DiffLine(line)


@dataclass
class Diff:
    hunks: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    # Per path, the new-side lines as (new_line_number, text) with the diff
    # marker stripped. Added and context lines are indexed (both are anchorable
    # new-file lines); deleted lines have no new-side number. Used by content
    # anchoring (ADR 0018) to match a finding's `quote` to its true line.
    lines: dict[str, list[tuple[int, str]]] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "Diff":
        d = cls()
        current_path: str | None = None
        for dl in _iter_diff_lines(text):
            if dl.new_path is not None:
                current_path = dl.new_path
                d.hunks.setdefault(current_path, [])
                d.lines.setdefault(current_path, [])
            elif current_path is None:
                continue
            elif dl.hunk is not None:
                start, count = dl.hunk
                if count:
                    d.hunks[current_path].append((start, start + count - 1))
            elif dl.new_lineno is not None:
                d.lines[current_path].append((dl.new_lineno, dl.raw[1:]))
        return d

    def match_quote(self, path: str, quote: str) -> list[int]:
        """New-side line numbers whose text matches `quote` (ADR 0018).

        Match strips leading and trailing whitespace on both sides and keeps
        internal whitespace exact: the agent reliably reproduces a line's content
        but often drifts on indentation, while collapsing internal spacing would
        merge distinct lines into one ambiguous match."""
        target = quote.strip()
        if not target:
            return []
        return [n for n, text in self.lines.get(path, []) if text.strip() == target]

    def is_anchored(self, path: str, line: int, end_line: int | None = None) -> bool:
        if path not in self.hunks:
            return False
        hunks = self.hunks[path]
        if end_line is None or end_line == line:
            return any(s <= line <= e for s, e in hunks)
        if end_line < line:
            return False
        return any(s <= line <= e and s <= end_line <= e for s, e in hunks)


LINE_NUM_SEP = "│"  # U+2502, distinct from a `|` inside code


def numbered_diff(text: str) -> str:
    """Prefix each new-side line with its new-file line number (ADR 0018, layer A).

    The agent reads `line` off the leading number instead of counting hunk lines.
    Only new-side lines (added and context) are numbered; deleted lines and all
    headers have no number, so the `+++` header is never read as an added line.
    The number is right-aligned to the widest in the diff."""
    lines = list(_iter_diff_lines(text))
    width = max((len(str(dl.new_lineno)) for dl in lines if dl.new_lineno is not None), default=1)
    return "".join(
        f"{('' if dl.new_lineno is None else str(dl.new_lineno)).rjust(width)}"
        f"{LINE_NUM_SEP}{dl.raw}\n"
        for dl in lines
    )


def drop_forbidden_combos(findings: list[dict]) -> tuple[list[dict], int]:
    kept: list[dict] = []
    dropped = 0
    for f in findings:
        combo = (f.get("severity", ""), f.get("type", ""))
        if combo in FORBIDDEN_COMBOS:
            dropped += 1
            continue
        kept.append(f)
    return kept, dropped


def _anchor_at(
    finding: dict, diff: Diff, path: str, target: int, emitted_line: int, end_line: int | None
) -> dict | None:
    """Anchor `finding` at `target`, shifting a range's end by target - emitted_line.

    Returns the corrected finding when the resulting span stays inside one hunk,
    else None to relocate."""
    new_end = end_line + (target - emitted_line) if end_line is not None else None
    if not diff.is_anchored(path, target, new_end):
        return None
    result = {**finding, "line": target}
    if end_line is not None:
        result["end_line"] = new_end
    return result


def resolve_finding(finding: dict, diff: Diff) -> dict | None:
    """Resolve a finding's inline anchor by content, the ADR 0018 confidence gate.

    Returns the finding (its `line`/`end_line` corrected to the quoted location)
    when it anchors inline with confidence, or None when it should relocate to
    the review body's `## Findings outside the diff` section. Never anchors inline
    on a guess.
    """
    path = finding.get("path", "")
    line = finding.get("line", 0)
    end_line = finding.get("end_line")
    quote = finding.get("quote")
    if quote and quote.strip():
        matches = diff.match_quote(path, quote)
        if len(matches) == 1:
            # The quote pins the start line. Shift a range's end by the same
            # delta (the miscount is a constant offset), then require the
            # corrected span to stay in one hunk, else relocate.
            return _anchor_at(finding, diff, path, matches[0], line, end_line)
        # Several matches: trust the emitted line only when it coincides with one
        # of them (corroboration); otherwise relocate rather than guess.
        if len(matches) > 1 and line in matches:
            return _anchor_at(finding, diff, path, line, line, end_line)
        return None
    # No quote: a region-level finding (file-level, an absence, a block) with no
    # single line to verify. The emitted line was read off the leading number,
    # not counted, so fall back to the range check on it (ADR 0018).
    if diff.is_anchored(path, line, end_line):
        return finding
    return None


def split_findings(findings: list[dict], diff: Diff) -> tuple[list[dict], list[dict]]:
    anchored: list[dict] = []
    unanchored: list[dict] = []
    for f in findings:
        resolved = resolve_finding(f, diff)
        if resolved is not None:
            anchored.append(resolved)
        else:
            unanchored.append(f)
    return anchored, unanchored


def _cmd_split(args: argparse.Namespace) -> int:
    payload = json.loads(args.payload.read_text())
    findings = payload.get("comments", [])
    kept, dropped = drop_forbidden_combos(findings)
    diff = Diff.parse(args.diff.read_text())
    anchored, unanchored = split_findings(kept, diff)

    args.anchored.write_text(json.dumps(anchored, indent=2) + "\n")
    args.unanchored.write_text(json.dumps(unanchored, indent=2) + "\n")
    print(f"dropped_forbidden_combo={dropped}")
    return 0


def _cmd_number(args: argparse.Namespace) -> int:
    sys.stdout.write(numbered_diff(args.diff.read_text()))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_split = sub.add_parser("split", help="route findings into anchored vs relocated")
    p_split.add_argument("payload", type=Path, help="path to extract_json.py output")
    p_split.add_argument("diff", type=Path, help="path to gh pr diff output")
    p_split.add_argument("--anchored", type=Path, required=True)
    p_split.add_argument("--unanchored", type=Path, required=True)
    p_split.set_defaults(func=_cmd_split)

    p_number = sub.add_parser("number", help="emit a line-numbered diff for the agent")
    p_number.add_argument("diff", type=Path, help="path to gh pr diff output")
    p_number.set_defaults(func=_cmd_number)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
