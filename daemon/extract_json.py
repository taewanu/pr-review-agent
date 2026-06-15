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
    # Exact source text of the flagged line, used to content-anchor the finding
    # to its true line (ADR 0018). Optional with graceful fallback: a missing
    # quote never fails the review (#44); the agent omits it only for region-level
    # findings with no single line to quote.
    quote: str | None = None

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


def extract(raw: str, *, validate_style: bool = True) -> ReviewPayload:
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
    # problem before the count noise (culling to N=cap doesn't fix em-dashes).
    # The editor stage (#133) parses the author payload with --no-style: the
    # voice gate moves behind the Editor (ADR 0016), so the author parse only
    # shapes the findings to hand on. The cap is not style and always applies.
    if validate_style:
        _validate_style(payload)
    if len(payload.comments) > MAX_FINDINGS:
        raise ExtractError(
            "cap-violation", f"too many findings: {len(payload.comments)} > cap {MAX_FINDINGS}"
        )
    return payload


def _validate_style(payload: ReviewPayload) -> None:
    """Post-hoc voice checks. Routes through ADR 0005 as a system failure.

    Shared rules live in voice.py (ADR 0010); voice.check_payload applies the
    summary-vs-body split once so the author parse and the post-Editor gate
    (apply_edits.py) stay identical. Fidelity is off here: the author emits a
    single fence the pipeline already JSON-parses, so it cannot smuggle an
    escaped entity the way a re-emitting Editor can (ADR 0016)."""
    violations = voice.check_payload(payload.summary, [c.body for c in payload.comments])
    if violations:
        raise ExtractError("style-violation", "; ".join(violations))


def main() -> int:
    args = sys.argv[1:]
    validate_style = "--no-style" not in args
    args = [a for a in args if a != "--no-style"]
    raw = Path(args[0]).read_text() if args else sys.stdin.read()
    try:
        payload = extract(raw, validate_style=validate_style)
    except ExtractError as exc:
        # First stderr line is parseable by review-pr.sh; remaining lines are
        # human-readable detail.
        print(f"category={exc.category}", file=sys.stderr)
        print(f"extract_json: {exc}", file=sys.stderr)
        return 1
    print(payload.model_dump_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
