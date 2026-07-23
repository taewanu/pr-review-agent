#!/usr/bin/env python3
"""Stamp an explicit 0-based `index` onto each finding in a review payload (#258).

The editor keys every decision by a finding's position in `comments[]`, and its
only cue was the array order. Across a large draft it miscounted and shifted
bodies onto the wrong findings, discarding the whole review. Stamping the index
lets the editor read it instead of counting.

Writes a separate copy: apply_edits.py reads the un-indexed author file, so the
extra key never reaches a posted comment. Reads stdin, writes stdout.
"""

from __future__ import annotations

import json
import sys


def index_findings(payload: dict) -> dict:
    comments = payload.get("comments", [])
    return {**payload, "comments": [{"index": i, **c} for i, c in enumerate(comments)]}


def main() -> int:
    payload = json.load(sys.stdin)
    json.dump(index_findings(payload), sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
