#!/usr/bin/env python3
"""Union parallel lens review payloads, dedup same-defect findings, gate, cap
(ADR 0023).

Each independent review lens emits its own payload. This merges them into one
*before* the ADR 0022 confidence gate, which is what lets redundancy lift recall:
the union carries a candidate one lens missed but another caught. Findings at
the same (path, line) are clustered by body-text similarity, not collapsed
outright: same-defect duplicates merge and keep the strongest score, so overlap
raises effective confidence instead of averaging it away, while two genuinely
different defects sharing a line both survive. The gate and cap then run once,
on the merged set, reusing extract_json so the filter semantics stay
single-sourced."""

import difflib
import sys
from pathlib import Path

# daemon/ is not a package and this runs by path, so add its own dir before
# importing the sibling parse/gate module (same idiom extract_json uses).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_json  # noqa: E402
from extract_json import ExtractError, Finding, ReviewPayload  # noqa: E402


def _merge_summaries(summaries: list[str]) -> str:
    """Provisional merged summary: the first lens's, since the editor reconciles
    the summary to the surviving findings downstream (ADR 0016). It only reaches
    the operator verbatim on a zero-finding review, where every lens's summary is
    an equivalent "nothing to flag" and the choice does not matter."""
    return next((s for s in summaries if s.strip()), summaries[0] if summaries else "")


# Two findings at the same (path, line) with body-text similarity at or above
# this ratio (difflib.SequenceMatcher) are treated as independent reports of
# the SAME defect and merged; below it, they're treated as distinct defects
# that happen to share a line and both survive. Chosen to cluster paraphrases
# of one defect (different lenses describe the same mechanism in their own
# words) while not merging two genuinely different bugs at one line.
_SAME_DEFECT_SIMILARITY_THRESHOLD = 0.5


def _merge_cluster(cluster: list[Finding]) -> Finding:
    """Collapse findings judged to be the same defect into one.

    The merged `confidence` is the max of the cluster's scored values, not min
    or mean, so overlap *raises* effective confidence: a location two lenses
    flag is not dropped for one lens's lukewarm score. `confidence is None`
    means unscored (never gated), not zero, so an unscored finding never
    stands in for a scored one, and a lone unscored finding is kept as-is."""
    scored = [f for f in cluster if f.confidence is not None]
    if not scored:
        return cluster[0]
    # The most confident finding stands for the cluster: its score is the max,
    # and its body is presumed the best-verified take on the shared defect.
    return max(scored, key=lambda f: f.confidence)


def _combine(group: list[Finding]) -> list[Finding]:
    """Cluster same-location findings by body-text similarity, merge each
    cluster, and return one Finding per cluster (not one Finding per location).

    Every finding in `group` shares a (path, line), but two lenses can flag two
    genuinely different defects on the same line (one lens's null-deref, a
    different lens's off-by-one). Collapsing the whole group to a single
    Finding would silently drop the distinct one; clustering by similarity
    keeps them apart unless their bodies actually describe the same defect."""
    clusters: list[list[Finding]] = []
    for finding in group:
        for cluster in clusters:
            if (
                difflib.SequenceMatcher(None, finding.body, cluster[0].body).ratio()
                >= _SAME_DEFECT_SIMILARITY_THRESHOLD
            ):
                cluster.append(finding)
                break
        else:
            clusters.append([finding])
    return [_merge_cluster(cluster) for cluster in clusters]


_SEVERITY_RANK = {"important": 0, "nit": 1, "pre_existing": 2}


def _truncate_to_cap(payload: ReviewPayload) -> None:
    """Keep the top max_findings() findings when the merged, deduped set exceeds
    the cap, instead of hard-failing the way extract_json.enforce_cap does for
    a single payload. The cap was sized for one generator; a union of up
    to 5 independently-capped lenses can legitimately exceed it even when every
    lens behaved correctly, so hard-failing here would discard every already-
    completed lens's output over a byproduct of merging, not a real defect.

    Ranks by severity (important > nit > pre_existing, the same order
    review-agent-default.md's own single-lens truncation convention uses) then
    by confidence descending. An unscored (None) finding ranks after every
    scored finding at the same severity: a demonstrated high score is a
    stronger signal for a forced truncation choice than an absent one, though
    None is still never *gated* elsewhere in the pipeline."""
    cap = extract_json.max_findings()
    if len(payload.comments) <= cap:
        return
    dropped = len(payload.comments) - cap
    payload.comments.sort(
        key=lambda f: (
            _SEVERITY_RANK[f.severity],
            -(f.confidence if f.confidence is not None else -1),
        )
    )
    payload.comments = payload.comments[:cap]
    print(
        f"merge-cap: truncated {dropped} finding(s) beyond {cap} post-merge",
        file=sys.stderr,
    )
    # Machine-parseable line (mirrors ExtractError's `category=` convention):
    # review-pr.sh greps this out of merge_findings.py's captured stderr and
    # passes it to apply_edits.py, which appends a count to the posted summary
    # (Anthropic's own code-review plugin does the same for its nit cap: report
    # at most N, mention the rest as a count in the summary) rather than the
    # drop happening silently past the operator's own view of the review.
    print(f"truncated_count={dropped}", file=sys.stderr)


def _dedup(findings: list[Finding]) -> list[Finding]:
    """Group unioned findings by (path, line), then dedup each group by
    same-defect similarity. A group can yield more than one surviving Finding
    if it contains genuinely distinct defects sharing a line.

    Insertion order is preserved (dict keeps first-seen key order), so the
    merged output is deterministic across runs given a fixed lens order."""
    groups: dict[tuple[str, int], list[Finding]] = {}
    for f in findings:
        groups.setdefault((f.path, f.line), []).append(f)
    result: list[Finding] = []
    for group in groups.values():
        result.extend(_combine(group))
    return result


def merge(
    raws: list[str], *, validate_style: bool = False, labels: list[str] | None = None
) -> ReviewPayload:
    """Parse each lens payload, union and dedup findings, then gate and truncate.

    One lens's malformed payload (a schema-invalid severity value, a missing
    fence, etc.) does not sink the others: dogfooding hit exactly this case,
    where one lens's bad output crashed the merge and discarded four other
    already-completed lenses' valid findings. Each payload is parsed
    independently; a lens that fails to parse is skipped (logged to stderr with
    its label and category) rather than aborting the whole merge, the same
    "one bad component doesn't sink everyone" principle _truncate_to_cap
    already applies to the cap. Only failing every lens is fatal.

    Style is off by default: the daemon runs lenses with --no-style (the voice
    gate lives behind the editor, ADR 0016), and the merged draft is handed to
    the editor, not posted directly."""
    if not raws:
        raise ExtractError("empty-stdout", "no lens payloads to merge")
    labels = labels if labels is not None else [f"lens {i}" for i in range(len(raws))]
    payloads = []
    for label, raw in zip(labels, raws, strict=True):
        try:
            payloads.append(extract_json.parse_payload(raw, validate_style=validate_style))
        except ExtractError as exc:
            print(f"merge-skip: {label} payload failed ({exc.category}): {exc}", file=sys.stderr)
    if not payloads:
        raise ExtractError(
            "all-lenses-failed", f"every one of {len(raws)} lens payload(s) failed to parse"
        )
    merged = ReviewPayload(
        summary=_merge_summaries([p.summary for p in payloads]),
        comments=_dedup([c for p in payloads for c in p.comments]),
    )
    extract_json._drop_low_confidence(merged)
    _truncate_to_cap(merged)
    return merged


def _label_from_path(path: str) -> str:
    """Derive a readable lens name from its raw-output filename, e.g.
    `.pr-review-raw-correctness.txt` -> "correctness", `.pr-review-raw.txt`
    (the default lens) -> "default". Best-effort for stderr diagnostics only;
    an unrecognized name just prints as-is."""
    name = Path(path).name
    prefix, suffix = ".pr-review-raw", ".txt"
    if name.startswith(prefix) and name.endswith(suffix):
        middle = name[len(prefix) : -len(suffix)]
        return middle.lstrip("-") or "default"
    return name


def main() -> int:
    """Merge the lens payload files named in argv, emit the merged JSON.

    Mirrors extract_json.main's contract: each argv entry is a file holding one
    lens's raw ```json-fenced output; the first stderr line is the parseable
    failure category for review-pr.sh's log_failure routing (ADR 0005)."""
    validate_style, paths = extract_json.parse_no_style_flag(sys.argv[1:])
    raws = [Path(p).read_text() for p in paths]
    labels = [_label_from_path(p) for p in paths]
    try:
        merged = merge(raws, validate_style=validate_style, labels=labels)
    except ExtractError as exc:
        print(f"category={exc.category}", file=sys.stderr)
        print(f"merge_findings: {exc}", file=sys.stderr)
        return 1
    print(merged.model_dump_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
