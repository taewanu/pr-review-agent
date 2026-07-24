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
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# daemon/ is not a package and this runs by path, so add its own dir before
# importing the sibling parse/gate module (same idiom extract_json uses).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_json  # noqa: E402
from extract_json import ExtractError, Finding, ReviewPayload  # noqa: E402

# Its own failure category, separate from all-lenses-failed (#231). The two look
# identical from the outside (no lens produced a parseable payload) but mean
# opposite things: all-lenses-failed says the agents ran and their output would
# not parse, a pipeline defect to go debug; session-limit says the agents never
# ran at all, an external quota with a known reset. Conflating them sends the
# operator to the wrong place and, worse, leaves the daemon retrying every cycle
# against a wall it cannot pass.
SESSION_LIMIT_CATEGORY = "session-limit"

# Matched from `hit your` onward: the sentinel opens with a contraction
# (`You've`), and which apostrophe character it uses is not worth guessing.
# `usage` is accepted alongside `session` because the two are used
# interchangeably for the same quota in Claude's own wording.
_SESSION_LIMIT_RE = re.compile(r"\bhit your (?:session|usage) limit\b", re.IGNORECASE)
# Everything after `resets`, punctuation and spacing between them ignored.
_SESSION_LIMIT_RESET_RE = re.compile(r"\bresets?\b[\s·:,-]*(?P<when>\S.*)\Z", re.IGNORECASE | re.S)
# A limited lens does no work and prints one line. Anything longer is a real
# payload, so this bound is what keeps the phrase quoted inside reviewed code
# from being read as a quota hit.
_SESSION_LIMIT_MAX_CHARS = 200


def session_limit_reset(raw: str) -> str | None:
    """Return the reset time carried by a session-limit sentinel in `raw`, or
    None when `raw` is not one. An empty string when the sentinel is present but
    the reset time cannot be read, which the caller treats as "limited, reset
    unknown" and backs off a fixed interval instead of until a parsed time.

    A lens that hit the subscription limit produces this instead of a payload:

        You've hit your session limit · resets 5pm

    Both halves of the answer are load-bearing. Returning None too readily leaves
    the daemon in the retry storm this exists to end. Returning non-None too
    readily is the worse error: a genuine pipeline defect would be relabelled as
    a quota pause, and polling would stop for hours while the real defect hides.

    Two things guard against that. The phrase match starts after the contraction,
    so the apostrophe (`'` or `'`) never has to be guessed. And the whole output
    must be short: a limited lens prints one line having done no work, while a
    review payload that merely failed to parse runs to thousands of characters,
    so the bound is what stops the phrase quoted inside reviewed code or a
    finding body from reading as a quota hit.
    """
    text = raw.strip()
    if not text or len(text) > _SESSION_LIMIT_MAX_CHARS:
        return None
    if not _SESSION_LIMIT_RE.search(text):
        return None
    reset = _SESSION_LIMIT_RESET_RE.search(text)
    return reset.group("when").strip() if reset else ""


# Local time throughout: the quota message is printed by the same machine the
# daemon runs on. Matches `5pm`, `3:30 a.m.`, and 24-hour `23:15`.
_CLOCK_12H_RE = re.compile(r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", re.IGNORECASE)
_CLOCK_24H_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
# So the first cycle back does not race the reset itself.
_RESET_SLACK_SECONDS = 60


def session_limit_deadline(reset_text: str, now: datetime) -> int | None:
    """Resolve a sentinel's human reset time to an absolute epoch second, or None
    when it carries no readable clock time and the caller should fall back to a
    fixed backoff.

    Resolving here rather than in the shell keeps the parsing beside the sentinel
    matcher that produced the text, under the same tests. `date` differs between
    BSD and GNU on exactly the flags this needs, which is the shell's problem to
    avoid, not to work around."""
    match = _CLOCK_12H_RE.search(reset_text)
    if match:
        # A 12-hour clock has no 13th hour; reading one as 13:00 would invent a
        # deadline out of a string nobody wrote.
        if not 1 <= int(match.group(1)) <= 12:
            return None
        hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "p" else 0)
        minute = int(match.group(2) or 0)
    elif match := _CLOCK_24H_RE.search(reset_text):
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # A reset time that already reads as past belongs to tomorrow: the window rolls.
    if reset <= now:
        reset += timedelta(days=1)
    return int(reset.timestamp()) + _RESET_SLACK_SECONDS


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
    review-agent-code.md's own single-payload truncation convention uses) then
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
    raws: list[str],
    *,
    validate_style: bool = False,
    labels: list[str] | None = None,
    session_limit_probe: str | None = None,
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
        # Every lens limited is a quota pause, not a pipeline defect (#231). The
        # test is the sentinel on every raw, never the count of failed lenses: a
        # real defect that broke all five would otherwise be misread as a quota
        # hit and stop polling for hours. A lens that timed out writes an empty
        # raw, which is not a sentinel, so a mixed run stays all-lenses-failed.
        resets = [session_limit_reset(raw) for raw in raws]
        if all(reset is not None for reset in resets):
            raise ExtractError(
                SESSION_LIMIT_CATEGORY,
                f"all {len(raws)} lenses hit the session limit, resets {resets[0] or 'unknown'}",
            )
        # Orchestrator dispatch (#299): the roles write only their payload
        # files, so a quota hit leaves those empty and the sentinel lands in
        # the orchestrator's own transcript instead. The probe is that
        # transcript, consulted only when NO payload parsed, so a mixed run
        # (a role that landed before the wall) stays a degraded success.
        if session_limit_probe is not None:
            probe_reset = session_limit_reset(session_limit_probe)
            if probe_reset is not None:
                raise ExtractError(
                    SESSION_LIMIT_CATEGORY,
                    f"generation hit the session limit, resets {probe_reset or 'unknown'}",
                )
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
    `.pr-review-raw-perf.txt` -> "perf", `.pr-review-raw.txt`
    (the code role) -> "code". Best-effort for stderr diagnostics only;
    an unrecognized name just prints as-is."""
    name = Path(path).name
    prefix, suffix = ".pr-review-raw", ".txt"
    if name.startswith(prefix) and name.endswith(suffix):
        middle = name[len(prefix) : -len(suffix)]
        return middle.lstrip("-") or "code"
    return name


def main() -> int:
    """Merge the lens payload files named in argv, emit the merged JSON.

    Mirrors extract_json.main's contract: each argv entry is a file holding one
    lens's raw ```json-fenced output; the first stderr line is the parseable
    failure category for review-pr.sh's log_failure routing (ADR 0005)."""
    argv = sys.argv[1:]
    probe_text = None
    if "--session-limit-probe" in argv:
        i = argv.index("--session-limit-probe")
        if i + 1 >= len(argv):
            print("category=unknown", file=sys.stderr)
            print("merge_findings: --session-limit-probe requires a path", file=sys.stderr)
            return 1
        try:
            probe_text = Path(argv[i + 1]).read_text()
        except OSError:
            # A missing transcript degrades to no probe: classification falls
            # back to all-lenses-failed, never aborts the merge itself.
            probe_text = None
        argv = argv[:i] + argv[i + 2 :]
    validate_style, paths = extract_json.parse_no_style_flag(argv)
    raws = [Path(p).read_text() for p in paths]
    labels = [_label_from_path(p) for p in paths]
    try:
        merged = merge(
            raws, validate_style=validate_style, labels=labels, session_limit_probe=probe_text
        )
    except ExtractError as exc:
        print(f"category={exc.category}", file=sys.stderr)
        if exc.category == SESSION_LIMIT_CATEGORY:
            # Second machine-readable line, so review-pr.sh pauses by comparing
            # two integers. Empty value means the sentinel was there but its time
            # was not readable; the caller falls back to a fixed interval.
            # The reset time lives wherever the sentinel was found: the raws
            # under per-role dispatch, the orchestrator transcript under #299.
            reset = session_limit_reset(raws[0]) if raws else None
            if reset is None and probe_text is not None:
                reset = session_limit_reset(probe_text)
            deadline = session_limit_deadline(reset or "", datetime.now())
            print(
                f"session_limit_deadline={deadline if deadline is not None else ''}",
                file=sys.stderr,
            )
        print(f"merge_findings: {exc}", file=sys.stderr)
        return 1
    print(merged.model_dump_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
