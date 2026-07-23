#!/usr/bin/env python3
"""Apply the Editor agent's decisions to the author payload, gate, emit (#133).

The review pipeline now has two agents (ADR 0016). The review agent emits a
draft payload (`{summary, comments[]}`), parsed by `extract_json.py --no-style`.
The Editor agent then emits `{summary, decisions[]}`, one decision per draft
finding keyed by index. This module applies those decisions to the draft,
assembles the final `{summary, comments[]}`, and runs the voice-plus-fidelity
gate that moved behind the Editor. Its stdout is the final payload the rest of
the pipeline (`anchor_findings.py`, `create-review.sh`) consumes, identical in
shape to what `extract_json.py` used to emit directly.

Kept findings carry the author's body by reference: the Editor names an index
and an action, never re-types a body it did not change, so the common path
cannot corrupt a body the Editor left alone. Only `rewrite` carries new text,
and that text plus the reconciled summary are the only fields fidelity-checked.
"""

from __future__ import annotations

import argparse
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


class ApplyError(Exception):
    """Categorised failure. `category` matches ADR 0005's failure table and is
    emitted to stderr so review-pr.sh can route it through log_failure."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


class EditDecision(BaseModel):
    index: int
    action: Literal["keep", "rewrite", "drop"]
    # Present only on rewrite; keep/drop reuse the author body or omit it, so any
    # body they carry is ignored (the by-reference contract, ADR 0016).
    body: str | None = None

    @model_validator(mode="after")
    def _rewrite_has_body(self) -> Self:
        if self.action == "rewrite" and not (self.body and self.body.strip()):
            raise ValueError(f"index {self.index}: rewrite requires a non-empty body")
        return self


class EditorPayload(BaseModel):
    summary: str
    decisions: list[EditDecision]


# Resolve the `list[EditDecision]` forward ref now. Under `from __future__ import
# annotations` the field annotation is a string, and this module is run by path
# (not registered in sys.modules), so pydantic cannot find the namespace lazily.
EditorPayload.model_rebuild()


def _parse_edits(raw: str) -> EditorPayload:
    if not raw.strip():
        raise ApplyError("edit-empty", "editor output is empty or whitespace-only")
    matches = FENCE_RE.findall(raw)
    if not matches:
        raise ApplyError("edit-no-fence", "no ```json fence found in editor output")
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        raise ApplyError("edit-parse-error", f"JSON decode failed: {exc}") from exc
    try:
        return EditorPayload.model_validate(data)
    except ValidationError as exc:
        raise ApplyError("edit-schema-invalid", str(exc)) from exc


def apply_edits(author: dict, edits: EditorPayload) -> dict:
    """Assemble the final payload by applying `edits` to the `author` draft.

    Every draft finding must be covered by exactly one decision, keyed by its
    0-based index; a missing, duplicated, or out-of-range index is an error
    rather than a silent drop. Survivors keep the draft's order.
    """
    comments = author.get("comments", [])
    n = len(comments)
    covered = sorted(d.index for d in edits.decisions)
    if covered != list(range(n)):
        raise ApplyError(
            "edit-coverage",
            f"decisions cover indices {covered}, expected one each of {list(range(n))}",
        )
    by_index = {d.index: d for d in edits.decisions}
    survivors = []
    dropped = []
    for i, comment in enumerate(comments):
        decision = by_index[i]
        if decision.action == "drop":
            dropped.append(i)
            continue
        if decision.action == "rewrite":
            survivors.append({**comment, "body": decision.body})
        else:
            survivors.append(comment)
    if dropped:
        # Surfaced by log_degradation_warnings' `editor-drop` prefix, the same way
        # the confidence gate's own drops are, so an editor drop leaves a trace in
        # the daemon log rather than vanishing silently (#259).
        print(
            f"editor-drop: dropped {len(dropped)} finding(s) at author index(es) {dropped}",
            file=sys.stderr,
        )
    return {"summary": edits.summary, "comments": survivors}


def _gate(payload: dict, *, check_fidelity: bool) -> None:
    """Fail on a corrupt payload; warn (never fail) on a cosmetic voice miss.

    A style violation — a forbidden opener, an em dash, a bullet count — is not a
    reason to discard a review that correctly found a real bug; it posts with a
    warning instead (CR treats tone as customizable, never a gate that drops
    findings). Only a reserialization corruption (an HTML-escaped character, a
    literal backslash-n; #133), which malforms the text the reader sees, stays
    fail-closed — and only post-Editor, where re-emission can introduce it. The
    warning goes to stderr so it reaches the daemon log without touching stdout,
    which carries the final payload.
    """
    summary = payload["summary"]
    bodies = [c["body"] for c in payload["comments"]]
    if check_fidelity and (corruptions := voice.fidelity_violations(summary, bodies)):
        raise ApplyError("edit-fidelity", "; ".join(corruptions))
    style = voice.check_payload(summary, bodies)  # cosmetic only; fidelity handled above
    if style:
        print(f"voice-warning: posting despite {'; '.join(style)}", file=sys.stderr)


def finalize(author: dict, edits_raw: str | None) -> dict:
    """Return the gated final payload.

    With editor output, apply the decisions and gate with fidelity on (the Editor
    re-emitted text). Without it (the zero-finding skip, ADR 0016 point 4), the
    author payload is final and the gate runs without fidelity, matching the
    author parse: the author never re-emits, so it cannot corrupt a body."""
    if edits_raw is None:
        _gate(author, check_fidelity=False)
        return author
    final = apply_edits(author, _parse_edits(edits_raw))
    _gate(final, check_fidelity=True)
    return final


def _append_note(payload: dict, note: str) -> dict:
    """Append one fixed line to the summary. Chrome authored here, not agent
    prose, so it runs after the Editor's gate and skips voice.py the way
    status_failure_reason's phrases do. Every note passed in is hand-kept 두괄식
    and em-dash-free, since the gate no longer guards it."""
    return {**payload, "summary": f"{payload['summary']}\n\n{note}"}


def append_truncation_note(payload: dict, truncated_count: int) -> dict:
    """Append a count of findings the merge-time cap silently dropped (ADR 0023),
    mirroring Anthropic's own code-review plugin convention of capping nits and
    mentioning the rest as a count rather than dropping them with no visible
    trace. A no-op when truncated_count is 0."""
    if truncated_count <= 0:
        return payload
    plural = "" if truncated_count == 1 else "s"
    # No severity word: the cap keeps the top-ranked findings and drops the
    # lowest-ranked tail, which is low-severity in a mixed set but is important
    # when a PR carries more important findings than the cap. The note only
    # knows the count, so it must not assert a severity it cannot verify (#194).
    return _append_note(
        payload,
        f"{truncated_count} additional finding{plural} omitted by the review cap.",
    )


def append_editor_bypass_note(payload: dict) -> dict:
    """Mark on the summary that the editorial pass was skipped (#258).

    Reached only on the post-author fallback: the editor's decisions were
    unusable, so the merged author draft is posting un-edited. A reader must be
    able to tell a bypassed review from a clean one, or a degraded review passes
    for a polished one. The internal failure category is logged to stderr, not
    surfaced here, since it is of no use to a PR author."""
    return _append_note(
        payload,
        "Editorial cleanup did not run on this review, so the findings below are the raw draft.",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--author", required=True, help="path to the author payload JSON")
    parser.add_argument("--edits", help="path to the Editor agent's raw stdout; omit to skip")
    parser.add_argument(
        "--truncated-count",
        type=int,
        default=0,
        help="findings dropped by merge_findings.py's cap truncation (ADR 0023)",
    )
    parser.add_argument(
        "--on-editor-error",
        choices=("discard", "post-author"),
        default="discard",
        help=(
            "what to do when the editor's decisions are unusable: 'discard' fails "
            "the review (default); 'post-author' posts the merged author draft with "
            "a bypass note (#258). Author-draft failures always fail regardless."
        ),
    )
    args = parser.parse_args()
    author = json.loads(Path(args.author).read_text())
    edits_raw = Path(args.edits).read_text() if args.edits else None

    def emit(payload: dict) -> int:
        payload = append_truncation_note(payload, args.truncated_count)
        print(json.dumps(payload))
        return 0

    try:
        return emit(finalize(author, edits_raw))
    except ApplyError as exc:
        # A failure applying the editor's decisions is recoverable: the author
        # draft is independently valid and posting it beats discarding a review
        # that already passed generation. finalize(author, None) gates fidelity
        # off, so a cosmetic voice miss warns rather than fails, the same fail-open
        # the zero-finding skip gets; a structurally broken author would still
        # raise, but merge_findings.py schema-validated it upstream.
        if args.on_editor_error == "post-author" and edits_raw is not None:
            print(f"warning=editor-bypassed category={exc.category}", file=sys.stderr)
            print(f"apply_edits: {exc}", file=sys.stderr)
            return emit(append_editor_bypass_note(finalize(author, None)))
        print(f"category={exc.category}", file=sys.stderr)
        print(f"apply_edits: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
