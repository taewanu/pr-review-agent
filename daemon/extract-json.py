#!/usr/bin/env python3
"""Parse the trailing ```json fence from stdin or argv[1], validate, emit JSON."""

import json
import re
import sys
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ValidationError, model_validator

# daemon/ is not a package and this script is run by path, so add its own dir to
# the import path before importing the shared voice rules (ADR 0010).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice  # noqa: E402

FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
MAX_FINDINGS = 10


class ExtractError(Exception):
    """Categorised extraction failure. `category` matches ADR 0005's failure table
    and is emitted to stderr so review-pr.sh can route it through log_failure."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


class Finding(BaseModel):
    path: str
    line: int
    end_line: int | None = None
    severity: Literal["important", "nit", "pre_existing"]
    type: Literal["bug", "refactor", "polish"]
    body: str

    @model_validator(mode="after")
    def _check_end_line(self) -> Self:
        if self.end_line is not None and self.end_line < self.line:
            raise ValueError(f"end_line ({self.end_line}) must be >= line ({self.line})")
        return self


class ReviewPayload(BaseModel):
    summary: str
    # `comments` defaults to `[]` so a payload like `{"summary": "..."}` validates
    # cleanly when the agent omits the field on a zero-finding review (intermittent
    # behavior tracked in #44). The prompt still asks for explicit `comments: []`,
    # but the pipeline absorbs the omission instead of failing schema-invalid.
    comments: list[Finding] = []


def extract(raw: str) -> ReviewPayload:
    if not raw.strip():
        raise ExtractError("empty-stdout", "input is empty or whitespace-only")
    matches = FENCE_RE.findall(raw)
    if not matches:
        raise ExtractError("no-fence", "no ```json fence found in input")
    # Separate JSON parse from schema validation so the failure category
    # distinguishes a malformed payload from a well-formed-but-invalid one.
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        raise ExtractError("parse-error", f"JSON decode failed: {exc}") from exc
    try:
        payload = ReviewPayload.model_validate(data)
    except ValidationError as exc:
        raise ExtractError("schema-invalid", str(exc)) from exc
    # Style first, cap second: with N>cap em-dash findings, surface the voice
    # problem before the count noise — culling to N=cap doesn't fix em-dashes.
    _validate_style(payload)
    if len(payload.comments) > MAX_FINDINGS:
        raise ExtractError(
            "cap-violation", f"too many findings: {len(payload.comments)} > cap {MAX_FINDINGS}"
        )
    return payload


def _validate_style(payload: ReviewPayload) -> None:
    """Post-hoc voice checks. Routes through ADR 0005 as a system failure.

    Shared rules live in voice.py (ADR 0010). The summary stays plain prose, so
    it forbids a leading bold (FORBIDDEN_SUMMARY_PREFIXES); comment bodies lead
    with a bold sentence, so they peel it before the opener scan (strip_bold)."""
    violations = voice.check_text(
        payload.summary, prefixes=voice.FORBIDDEN_SUMMARY_PREFIXES, label="summary"
    )
    for i, c in enumerate(payload.comments):
        violations += voice.check_text(
            c.body,
            prefixes=voice.FORBIDDEN_PREFIXES,
            strip_bold=True,
            check_bullets=True,
            label=f"comments[{i}].body",
        )
    if violations:
        raise ExtractError("style-violation", "; ".join(violations))


def main() -> int:
    raw = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else sys.stdin.read()
    try:
        payload = extract(raw)
    except ExtractError as exc:
        # First stderr line is parseable by review-pr.sh; remaining lines are
        # human-readable detail.
        print(f"category={exc.category}", file=sys.stderr)
        print(f"extract-json: {exc}", file=sys.stderr)
        return 1
    print(payload.model_dump_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
