#!/usr/bin/env python3
"""Split a review payload into anchored and unanchored findings.

The diff arg is accepted for forward-compat with hunk-aware anchoring; the
current implementation ignores it and marks every finding as anchored.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path, help="path to extract-json.py output")
    parser.add_argument("diff", type=Path, help="path to gh pr diff output (unused in Slice 1)")
    parser.add_argument("--anchored", type=Path, required=True)
    parser.add_argument("--unanchored", type=Path, required=True)
    args = parser.parse_args()

    payload = json.loads(args.payload.read_text())
    comments = payload.get("comments", [])

    args.anchored.write_text(json.dumps(comments, indent=2) + "\n")
    args.unanchored.write_text(json.dumps([], indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
