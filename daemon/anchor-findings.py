#!/usr/bin/env python3
"""Split a review payload into anchored (in-diff) and unanchored findings.

Parses `gh pr diff` output for the new-file hunks (GitHub's PR Review API
anchors comments to new-file lines, so old-file ranges aren't used). A finding
is anchored when its `path` is in the diff AND its `line` (and optional
`end_line`) falls inside a hunk on the new side. Range findings must have both
endpoints in the same hunk — GitHub rejects cross-hunk ranges with 422.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<old>.+) b/(?P<new>.+)$")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
BINARY_RE = re.compile(r"^Binary files .+ differ$")


@dataclass
class Diff:
    hunks: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "Diff":
        d = cls()
        current_path: str | None = None
        for line in text.splitlines():
            m = DIFF_GIT_RE.match(line)
            if m:
                current_path = m.group("new")
                d.hunks.setdefault(current_path, [])
                continue
            if current_path is None:
                continue
            if BINARY_RE.match(line):
                continue
            m = HUNK_RE.match(line)
            if m:
                start = int(m.group("start"))
                count = int(m.group("count")) if m.group("count") else 1
                if count == 0:
                    continue
                d.hunks[current_path].append((start, start + count - 1))
        return d

    def is_anchored(self, path: str, line: int, end_line: int | None = None) -> bool:
        if path not in self.hunks:
            return False
        hunks = self.hunks[path]
        if end_line is None or end_line == line:
            return any(s <= line <= e for s, e in hunks)
        if end_line < line:
            return False
        return any(s <= line <= e and s <= end_line <= e for s, e in hunks)


def split_findings(findings: list[dict], diff: Diff) -> tuple[list[dict], list[dict]]:
    anchored: list[dict] = []
    unanchored: list[dict] = []
    for f in findings:
        path = f.get("path", "")
        line = f.get("line", 0)
        end_line = f.get("end_line")
        if diff.is_anchored(path, line, end_line):
            anchored.append(f)
        else:
            unanchored.append(f)
    return anchored, unanchored


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path, help="path to extract-json.py output")
    parser.add_argument("diff", type=Path, help="path to gh pr diff output")
    parser.add_argument("--anchored", type=Path, required=True)
    parser.add_argument("--unanchored", type=Path, required=True)
    args = parser.parse_args()

    payload = json.loads(args.payload.read_text())
    findings = payload.get("comments", [])
    diff = Diff.parse(args.diff.read_text())
    anchored, unanchored = split_findings(findings, diff)

    args.anchored.write_text(json.dumps(anchored, indent=2) + "\n")
    args.unanchored.write_text(json.dumps(unanchored, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
