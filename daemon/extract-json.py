#!/usr/bin/env python3
"""Parse the trailing ```json fence from stdin or argv[1], validate, emit JSON."""

import json
import re
import sys
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ValidationError, model_validator

FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
MAX_FINDINGS = 10
EM_DASH = "—"
# Openers the voice prompt forbids. Trailing space distinguishes "This "
# (demonstrative opener) from words like "Think". Body and summary diverge per
# ADR 0002: bodies now lead with a bold sentence, so `**` is permitted there
# (and in fact required by the shape). Summary stays plain prose.
FORBIDDEN_PREFIXES = (
    "This ",
    "The ",
    "It ",
    "Worth ",
    "Suggest ",
    "Please ",
    "Consider ",
    "Maybe ",
)
FORBIDDEN_SUMMARY_PREFIXES = ("**",) + FORBIDDEN_PREFIXES


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
    comments: list[Finding]


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


def _forbidden_prefix(
    text: str, prefixes: tuple[str, ...], *, strip_bold: bool = False
) -> str | None:
    stripped = text.lstrip()
    # ADR 0002 bodies lead with `**…**`. Peel a leading `**` before the prefix
    # scan so word-level openers caught on plain prose still trip inside the
    # bold (`**This carries the wrong invariant.**` must fail like the plain
    # form). Summary forbids `**` outright, so no peel there.
    if strip_bold and stripped.startswith("**"):
        stripped = stripped[2:]
    for prefix in prefixes:
        if stripped.startswith(prefix):
            return prefix
    return None


def _validate_style(payload: ReviewPayload) -> None:
    """Post-hoc voice checks. Routes through ADR 0005 as a system failure."""
    violations: list[str] = []
    if EM_DASH in payload.summary:
        violations.append("summary contains em dash")
    if (prefix := _forbidden_prefix(payload.summary, FORBIDDEN_SUMMARY_PREFIXES)) is not None:
        violations.append(f"summary opens with forbidden prefix {prefix.rstrip()!r}")
    for i, c in enumerate(payload.comments):
        if EM_DASH in c.body:
            violations.append(f"comments[{i}].body contains em dash")
        if (prefix := _forbidden_prefix(c.body, FORBIDDEN_PREFIXES, strip_bold=True)) is not None:
            violations.append(f"comments[{i}].body opens with forbidden prefix {prefix.rstrip()!r}")
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
