#!/usr/bin/env python3
"""Parse the trailing ```json fence from stdin or argv[1], validate, emit JSON."""

import json
import os
import re
import sys
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, ValidationError, model_validator

# daemon/ is not a package and this script is run by path, so add its own dir to
# the import path before importing the shared voice rules (ADR 0010).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice  # noqa: E402

FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
# Findings cap; env MAX_FINDINGS overrides (#199), read per-call via
# max_findings() so review-pr.sh's export lands the same way as
# CONFIDENCE_THRESHOLD's.
DEFAULT_MAX_FINDINGS = 10
# ADR 0022. The precision floor for the confidence gate; env CONFIDENCE_THRESHOLD
# overrides. 80 follows the Anthropic code-review plugin default, a dogfood
# starting point, not a settled value.
DEFAULT_CONFIDENCE_THRESHOLD = 80


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
    type: Literal["bug", "refactor", "polish", "intent"]
    body: str
    # Exact source text of the flagged line, used to content-anchor the finding
    # to its true line (ADR 0018). Optional with graceful fallback: a missing
    # quote never fails the review (#44); the agent omits it only for region-level
    # findings with no single line to quote.
    quote: str | None = None
    # Confidence 0-100 that the finding is real and worth surfacing (ADR 0022).
    # Optional like `quote`: an unscored finding (None) is never dropped by the
    # gate, so an older payload or a #44 omission is kept, not culled.
    confidence: int | None = Field(default=None, ge=0, le=100)

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


def parse_payload(raw: str, *, validate_style: bool = True) -> ReviewPayload:
    """Fence-extract, JSON-parse, and schema-validate a single agent payload.

    Each finding in `comments` validates independently (ADR 0024 follow-up): one
    finding with a bad field (a stray `severity: "minor"`, observed live sharing
    a payload with an otherwise-valid, high-confidence finding) is dropped and
    logged, not treated as invalidating every other finding the same lens
    produced. Payload-level fields (`summary` itself, `comments` not being a
    list) still fail the whole payload: there is no per-item structure to
    salvage there the way there is for one bad entry in a list of many.

    The parse stage only, without the confidence gate or the cap: those filter
    the finding *set* and must run after ADR 0023 unions candidates across
    lenses, so a candidate is not gated per-lens before overlap can raise its
    score. `extract()` composes this with the gate and cap for the single-agent
    path; `merge_findings.py` calls it per lens and gates the union."""
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
    if not isinstance(data, dict):
        raise ExtractError(
            "schema-invalid", f"top-level JSON must be an object, got {type(data).__name__}"
        )
    summary = data.get("summary")
    if not isinstance(summary, str):
        raise ExtractError("schema-invalid", "'summary' must be a string")
    raw_comments = data.get("comments", [])
    if not isinstance(raw_comments, list):
        raise ExtractError(
            "schema-invalid", f"'comments' must be a list, got {type(raw_comments).__name__}"
        )
    findings: list[Finding] = []
    for i, item in enumerate(raw_comments):
        try:
            findings.append(Finding.model_validate(item))
        except ValidationError as exc:
            print(f"finding-skip: comments[{i}] failed validation: {exc}", file=sys.stderr)
    payload = ReviewPayload(summary=summary, comments=findings)
    if validate_style:
        _validate_style(payload)
    return payload


def max_findings() -> int:
    """The findings cap, from env MAX_FINDINGS or the default (#199).

    Same degradation contract as _confidence_threshold below: a malformed
    value falls back to the default with a stderr warning instead of raising,
    so one operator typo cannot crash every review tick."""
    raw = os.environ.get("MAX_FINDINGS")
    if raw is None:
        return DEFAULT_MAX_FINDINGS
    try:
        return int(raw)
    except ValueError:
        print(
            f"findings-cap: ignoring non-integer MAX_FINDINGS={raw!r}, "
            f"using default {DEFAULT_MAX_FINDINGS}",
            file=sys.stderr,
        )
        return DEFAULT_MAX_FINDINGS


def enforce_cap(payload: ReviewPayload) -> None:
    """Raise cap-violation if the finding count exceeds the findings cap."""
    cap = max_findings()
    if len(payload.comments) > cap:
        raise ExtractError(
            "cap-violation", f"too many findings: {len(payload.comments)} > cap {cap}"
        )


def extract(raw: str, *, validate_style: bool = True) -> ReviewPayload:
    # Order: style, then the confidence gate (ADR 0022), then the cap. Style
    # first so an em-dash finding surfaces before count noise (culling to N=cap
    # doesn't fix em-dashes); the gate before the cap so a low-confidence finding
    # never takes a cap slot. The editor stage (#133) parses the author payload
    # with --no-style: the voice gate moves behind the Editor (ADR 0016), so the
    # author parse only shapes the findings to hand on. The gate and cap are not
    # style and always apply.
    payload = parse_payload(raw, validate_style=validate_style)
    _drop_low_confidence(payload)
    enforce_cap(payload)
    return payload


def _confidence_threshold() -> int:
    """The confidence gate's cutoff, from env CONFIDENCE_THRESHOLD or the default.

    A malformed value (non-integer, empty) falls back to the default with a
    warning rather than raising: this gate runs outside extract()'s try/except,
    so an uncaught ValueError would escape as an uncategorized crash (a stack
    trace, `category=unknown`) and break every review tick from one operator
    typo. Degrading to the default keeps reviews flowing, matching the shell
    `:-` idiom's tolerance. An out-of-range integer is a legitimate operator
    choice (a low value keeps everything, a high one drops all scored), not
    malformed, so it is honored as-is."""
    raw = os.environ.get("CONFIDENCE_THRESHOLD")
    if raw is None:
        return DEFAULT_CONFIDENCE_THRESHOLD
    try:
        return int(raw)
    except ValueError:
        print(
            f"confidence-gate: ignoring non-integer CONFIDENCE_THRESHOLD={raw!r}, "
            f"using default {DEFAULT_CONFIDENCE_THRESHOLD}",
            file=sys.stderr,
        )
        return DEFAULT_CONFIDENCE_THRESHOLD


def _drop_low_confidence(payload: ReviewPayload) -> None:
    """Drop findings scored below the confidence threshold (ADR 0022).

    This deterministic cull, not the agent's self-censorship, is where low
    confidence findings are removed, which is what lets the prompt tell the agent
    to generate wide and score honestly. Runs before the cap so a dropped finding
    never takes a cap slot. A finding with no score (confidence None) is kept:
    absence means not-scored, not zero, so older payloads and #44 omissions
    survive. Env CONFIDENCE_THRESHOLD overrides the default."""
    threshold = _confidence_threshold()
    kept = [c for c in payload.comments if c.confidence is None or c.confidence >= threshold]
    dropped = len(payload.comments) - len(kept)
    if dropped:
        print(f"confidence-gate: dropped {dropped} finding(s) below {threshold}", file=sys.stderr)
    payload.comments = kept


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


def parse_no_style_flag(argv: list[str]) -> tuple[bool, list[str]]:
    """Split a `--no-style` flag out of argv. Shared by this module's own
    main() and merge_findings.py's, both of which take the flag the same way:
    `validate_style` is False when present, and it is stripped from the
    returned positional args."""
    return "--no-style" not in argv, [a for a in argv if a != "--no-style"]


def main() -> int:
    validate_style, args = parse_no_style_flag(sys.argv[1:])
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
