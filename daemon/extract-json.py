#!/usr/bin/env python3
"""Parse the trailing ```json fence from claude -p stdout, validate the payload.

Slice 1: happy-path only. Reads stdin (or a file path passed as argv[1]),
finds the last ```json ... ``` fenced block, validates the payload against the
ADR 0002 schema, and writes normalized JSON to stdout.

Per ADR 0005, schema/parse failures are system failures: log to stderr and exit
non-zero. The richer error taxonomy and per-failure messages land in Slice 4.
"""

import re
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
MAX_FINDINGS = 10


class Finding(BaseModel):
    path: str
    line: int
    severity: Literal["important", "nit", "pre_existing"]
    type: Literal["bug", "refactor", "polish"]
    body: str


class ReviewPayload(BaseModel):
    summary: str
    comments: list[Finding]


def extract(raw: str) -> ReviewPayload:
    matches = FENCE_RE.findall(raw)
    if not matches:
        raise ValueError("no ```json fence found in input")
    payload = ReviewPayload.model_validate_json(matches[-1])
    if len(payload.comments) > MAX_FINDINGS:
        raise ValueError(f"too many findings: {len(payload.comments)} > cap {MAX_FINDINGS}")
    return payload


def main() -> int:
    raw = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else sys.stdin.read()
    try:
        payload = extract(raw)
    except Exception as exc:
        print(f"extract-json: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(payload.model_dump_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
