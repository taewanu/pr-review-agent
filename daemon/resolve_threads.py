#!/usr/bin/env python3
"""Commit-driven thread resolution: select, judge, then stamp the Finding and resolve.

Originated as note-and-resolve (#125, ADR 0017); the resolution model is now an
in-place stamp on the Finding's own comment (ADR 0019, #159).

The review path runs this on each new HEAD SHA to find prior Findings a commit
may have fixed without any Operator reply, stamp the Finding's comment resolved,
and resolve the thread. Safe-biased throughout: any uncertainty leaves the thread
open, since a wrongly-closed live Finding is the dangerous failure and a
wrongly-left-open fixed one is recoverable by a hand click (ADR 0017).

Pure selection/format entry points (unit-tested without gh):

  select_candidates: open, daemon-owned threads, not yet carrying a resolution
    stamp, that a commit may have fixed. A thread whose Finding line this increment's
    diff touched is always judged; up to `untouched_cap` threads whose line it did
    not touch are judged too, since a fix can land away from the flagged line (#172,
    e.g. a missing test added in a new file). The line tested is the thread's
    `originalLine` (its coordinate in the commit the Finding was posted against),
    matched against the OLD side of `git diff LAST_SHA..HEAD`. GitHub's GraphQL
    `line` is null exactly when a thread goes outdated, which is precisely when its
    code changed (the case we must catch), so `line` is unusable here and
    `originalLine` is the only surviving coordinate. An open, un-stamped Finding's
    code is untouched since it was posted (a prior increment would have judged it
    otherwise), so `originalLine` stays a valid old-side coordinate across ticks.

  select_retry_threads: an open, daemon-owned thread that already carries a
    resolution stamp. The stamp landed but its resolve dropped (rate-limit); this
    re-resolves it with no re-judgment and no second stamp (ADR 0017 §4). The
    has_resolution_stamp exclusion in select_candidates is what keeps the two disjoint.

  parse_verdict: the Fix-check agent's {fixed, rationale}. Any parse or schema
    failure returns fixed=False, so a malformed judgment leaves the thread open.

  degrade_rationale: the rationale a stamp will carry, voice-cleaned. A positive fix
    verdict resolves regardless of wording (#168), so an off-voice rationale is degraded
    to a clean stand-in, never dropped; the resolve is gated on the judgment alone.

  build_stamp / append_stamp: the stamp text (built from the voice-clean rationale) and
    the idempotent append onto the Finding comment's existing body.

The `act` subcommand stamps each resolved Finding's comment in place via
resolution.update_review_comment and resolves both the freshly-stamped and the
retry threads. The stamp is a silent edit (no notification): the trace is kept, the
notification dropped as redundant with the Operator's own pre-merge review (ADR 0019).
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# daemon/ is not a package and this script is run by path, so add its own dir to
# the import path before importing the shared resolution toolkit, the blob-link
# helper, and the voice rules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolution  # noqa: E402
import voice  # noqa: E402
from links import build_blob_link  # noqa: E402
from resolution import (  # noqa: E402
    append_stamp,
    build_stamp,
)

DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<old>.+) b/(?P<new>.+)$")
# Old-side range of a hunk: `@@ -<start>,<count> +... @@`. Count defaults to 1
# when omitted; a count of 0 is a pure insertion with no old-side lines.
HUNK_OLD_RE = re.compile(r"^@@ -(?P<start>\d+)(?:,(?P<count>\d+))? \+\d+(?:,\d+)? @@")

# Provenance marker on every daemon-authored comment (ADR 0010 §3). Mirrors
# lib.sh's PROVENANCE_TAG and create_reply.py's MARKER; test_provenance_tag.py
# pins all three identical. Used here to tell a daemon Finding thread from the
# Operator's own manual review comment, so only the daemon's own threads can
# become resolution candidates.
PROVENANCE_MARKER = "🤖 _pr-review-agent_"


@dataclass
class IncrementDiff:
    """Old-side hunk ranges of an increment diff, keyed by file path.

    Distinct from anchor_findings.Diff, which parses the NEW side to anchor fresh
    Findings; here a Finding from a prior review is matched against the OLD side
    to ask whether this increment touched the line it was posted on.
    """

    ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "IncrementDiff":
        d = cls()
        paths: list[str] = []
        for line in text.splitlines():
            m = DIFF_GIT_RE.match(line)
            if m:
                # Register under both old and new path so a thread anchored to
                # either name (renames) still matches; they coincide otherwise.
                paths = [m.group("old"), m.group("new")]
                for p in paths:
                    d.ranges.setdefault(p, [])
                continue
            if not paths:
                continue
            m = HUNK_OLD_RE.match(line)
            if m:
                start = int(m.group("start"))
                count = int(m.group("count")) if m.group("count") else 1
                if count == 0:
                    continue
                for p in paths:
                    d.ranges[p].append((start, start + count - 1))
        return d

    def touched(self, path: str, line: int, start_line: int | None = None) -> bool:
        """Whether [start_line or line .. line] overlaps an old-side hunk for path."""
        if path not in self.ranges or line <= 0:
            return False
        lo = start_line if (start_line and start_line > 0) else line
        hi = line
        if lo > hi:
            lo, hi = hi, lo
        return any(s <= hi and lo <= e for s, e in self.ranges[path])


def select_candidates(
    threads: list[dict],
    diff: IncrementDiff,
    operator: str,
    all_open: bool = False,
    untouched_cap: int = 0,
) -> list[dict]:
    """Open, daemon-owned threads of prior Findings the new HEAD might have fixed.

    Every candidate is open (not resolved on GitHub), daemon-owned (root comment
    authored by the operator AND carrying the Provenance marker, so an Operator's
    own manual comment never qualifies), and not yet stamped (a thread judged fixed
    on an earlier tick goes to select_retry_threads, never re-judged here).

    Among those, a Finding is `touched` when its `original_line` (creation-side
    coordinate) falls inside an old-side hunk of this increment's diff. Touched
    threads are always judged: the increment changed the exact line the Finding
    sits on, the strongest "maybe fixed" signal.

    `untouched` threads are the #172 broadening: a fix can land away from the
    flagged line (a missing test added in a new file, a guard added elsewhere), so
    the touched test alone misses a whole class. We judge up to `untouched_cap` of
    them, the cost guard bounding the extra fix-check calls per tick.

    `all_open` lifts the cap and judges every open thread. The review path sets it
    on a force-push or rebase, where the increment diff can't be computed and the
    touched/untouched split is meaningless (ADR 0017 §1's "full PR diff" scope).
    """
    touched: list[dict] = []
    untouched: list[dict] = []
    for t in threads:
        if t.get("is_resolved"):
            continue
        if t.get("root_author") != operator:
            continue
        body = t.get("root_body") or ""
        if PROVENANCE_MARKER not in body:
            continue
        if t.get("has_resolution_stamp"):
            continue
        path = t.get("path") or ""
        line = t.get("original_line")
        if not isinstance(line, int):
            continue
        start_line = t.get("original_start_line")
        if not isinstance(start_line, int):
            start_line = None
        entry = {
            "thread_id": t["thread_id"],
            "comment_id": t.get("root_comment_id"),
            "path": path,
            "line": line,
            # HEAD-remapped coordinate for the stamp's blob link, kept distinct from
            # `line` (the creation-side coordinate the touched test above uses). Null
            # when the thread is outdated, which head_link_range turns into no anchor.
            "head_line": t.get("head_line"),
            "head_start_line": t.get("head_start_line"),
            "finding_body": body,
        }
        if diff.touched(path, line, start_line):
            touched.append(entry)
        else:
            untouched.append(entry)

    effective_cap = len(untouched) if all_open else untouched_cap
    # Judge file-touched threads first: a fix landing elsewhere in the Finding's
    # own file is likelier than one in a file this increment never opened. Then
    # truncate to the cost budget.
    chosen_untouched: list[dict] = sorted(untouched, key=lambda e: e["path"] not in diff.ranges)[
        :effective_cap
    ]

    dropped = len(untouched) - len(chosen_untouched)
    if dropped > 0:
        print(
            f"resolution candidates: {len(touched)} touched + "
            f"{len(chosen_untouched)} untouched judged, {dropped} untouched capped "
            f"(untouched_cap={untouched_cap})",
            file=sys.stderr,
        )
    return touched + chosen_untouched


def select_retry_threads(threads: list[dict], operator: str) -> list[dict]:
    """Open, daemon-owned threads already carrying a resolution stamp (ADR 0017 §4).

    These are the stuck-open state: the stamp landed on an earlier tick but the
    resolve mutation dropped under rate-limit. They re-resolve here with no
    re-judgment and no second stamp; the stamp is the work, the resolve is the
    retry. Disjoint from select_candidates by construction: a stamped thread is
    excluded there (has_resolution_stamp) and selected here. No diff filter, since after a
    fix the Finding's `original_line` no longer shares a coordinate space with the
    new increment's diff, so candidacy by diff would never re-catch it.
    """
    retry: list[dict] = []
    for t in threads:
        if t.get("is_resolved"):
            continue
        if t.get("root_author") != operator:
            continue
        if PROVENANCE_MARKER not in (t.get("root_body") or ""):
            continue
        if not t.get("has_resolution_stamp"):
            continue
        retry.append({"thread_id": t["thread_id"]})
    return retry


def parse_verdict(raw: str) -> dict:
    """Parse the Fix-check agent's stdout into {fixed: bool, rationale: str}.

    Same last-```json-fence convention as the review and reply agents. Safe-biased
    (ADR 0017): any parse failure or missing/invalid field returns fixed=False, so
    a malformed judgment leaves the thread open rather than resolving it.
    """
    matches = re.findall(r"```json\s*\n(.*?)\n```", raw, re.DOTALL)
    if not matches:
        return {"fixed": False, "rationale": "no json fence in fix-check output"}
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        return {"fixed": False, "rationale": f"verdict parse failed: {exc}"}
    fixed = data.get("fixed")
    rationale = data.get("rationale")
    if not isinstance(fixed, bool) or not isinstance(rationale, str) or not rationale.strip():
        return {"fixed": False, "rationale": "verdict missing 'fixed' bool or 'rationale' text"}
    return {"fixed": fixed, "rationale": rationale.strip()}


# A voice-clean stand-in for a fix rationale that trips the voice gate (#168). The
# stamp is a silent in-place edit (ADR 0019), so losing the agent's exact wording on
# the rare miss costs little; the resolve must proceed either way, and the daemon's
# own PR text stays voice-clean (ADR 0010).
FALLBACK_RATIONALE = "fix verified against the updated code"


def _rationale_violations(rationale: str) -> list[str]:
    """Voice violations of a one-line fix rationale: the same gate the stamp's visible
    text must pass (forbidden opener, em dash, task ref, bullet count). Empty after
    the caller's strip counts as a violation, since an empty rationale leaves the stamp
    lead with a dangling colon."""
    if not rationale:
        return ["empty rationale"]
    return voice.check_text(
        rationale,
        prefixes=voice.FORBIDDEN_PREFIXES,
        check_bullets=True,
        label="resolution stamp",
    )


def degrade_rationale(rationale: str) -> tuple[str, str | None]:
    """Return a voice-clean rationale for the stamp, plus a notice of what changed.

    A positive fix verdict must resolve regardless of the stamp's wording (#168), so a
    rationale that trips the voice gate is rewritten rather than dropped. Returns
    (rationale_to_use, notice): `notice` is None when the input was already clean (the
    agent's words are kept verbatim), else a one-line description of the rewrite for the
    foreground trace. `_rationale_violations` is the gate; FALLBACK_RATIONALE the clean
    stand-in."""
    rationale = rationale.strip()
    violations = _rationale_violations(rationale)
    if not violations:
        return rationale, None
    return FALLBACK_RATIONALE, f"rationale rewritten to fallback ({'; '.join(violations)})"


def head_link_range(
    head_line: int | None, head_start_line: int | None
) -> tuple[int | None, int | None]:
    """The (line, end_line) to feed build_blob_link for a resolution stamp's HEAD blob
    link, from a thread's HEAD-remapped coordinate.

    `head_line`/`head_start_line` are GitHub's `line`/`startLine` at HEAD (lib.sh):
    where the Finding's anchored code now lives after the increment. The stamp link
    must use this, not the creation-side `original_line`, or it anchors a HEAD blob on
    a coordinate a fix has since shifted and lands on unrelated code (the bug this
    fixes: `original_line` 413 was a flagged line at creation but a bare `}` once the
    fix inserted lines above it). GitHub nulls `head_line` exactly when a thread is
    outdated and it can no longer map the Finding to HEAD; there is then no honest HEAD
    line to point at, so the link must drop its anchor: a None line makes
    build_blob_link return None, and build_stamp then renders "✅ _Resolved_" with no link.

    Return (line, end_line) for build_blob_link(..., line, end_line): a (start, end)
    range when the Finding spans multiple lines, a single line otherwise, and
    (None, None) when there is no HEAD coordinate. build_blob_link already collapses
    end == start to a single `#L`, so an equal pair needs no special case."""
    if head_line is None:
        return None, None
    if head_start_line is not None and head_start_line != head_line:
        return head_start_line, head_line
    return head_line, None


def _vet_notes(
    notes: list[dict], head_owner: str, head_repo: str, head_sha: str
) -> tuple[list[dict], list[str]]:
    """Build each note's voice-clean Resolution stamp, returning (postable, degraded).

    Every note becomes postable: a positive fix verdict must resolve (ADR 0019, #168),
    so the stamp's wording can never block it. A rationale that trips the voice gate is
    degraded (degrade_rationale) to a clean stand-in rather than dropped. `postable`
    carries the edit target per note (`thread_id`, `comment_id` the Finding's root
    comment node id, `body` its current body to append below, and the built `stamp`);
    `degraded` is a log line per note whose rationale was rewritten. The gate runs on the
    agent's rationale (not the whole stamp), so the fixed lead and the sentinel never
    trip it."""
    postable: list[dict] = []
    degraded: list[str] = []
    for n in notes:
        tid = n["thread_id"]
        rationale, notice = degrade_rationale(n.get("rationale") or "")
        if notice:
            degraded.append(f"resolution stamp {tid}: {notice}")
        link_line, link_end = head_link_range(n.get("head_line"), n.get("head_start_line"))
        link = build_blob_link(head_owner, head_repo, head_sha, n.get("path"), link_line, link_end)
        postable.append(
            {
                "thread_id": tid,
                "comment_id": n.get("comment_id"),
                "body": n.get("finding_body") or "",
                "stamp": build_stamp(rationale, link),
            }
        )
    return postable, degraded


def post_and_resolve(
    notes: list[dict],
    retry: list[dict],
    *,
    head_owner: str,
    head_repo: str,
    head_sha: str,
) -> dict:
    """Stamp each resolved Finding's comment in place, then resolve its thread plus
    every retry thread (#159, ADR 0019).

    Stamp-then-resolve order and best-effort throughout (ADR 0017 §4): the stamp is
    the audit trace and must land first, so a thread is only resolved after its
    comment edit succeeds. append_stamp returns None when the comment is already
    stamped (a re-run), in which case the edit is skipped but the thread is still
    resolved (the stuck-open case the retry path also handles). An update failure
    leaves the thread open and unstamped for a later tick. Resolve is idempotent, so
    a retry thread re-resolves harmlessly. No batched review here, unlike the reply
    path: the stamp is a silent in-place edit, not a notifying comment (ADR 0019)."""
    postable, degraded = _vet_notes(notes, head_owner, head_repo, head_sha)
    for line in degraded:
        print(f"resolution-stamp degraded: {line}", file=sys.stderr)

    stamped_threads = []
    for note in postable:
        tid = note["thread_id"]
        if not note["comment_id"]:
            # No edit target (a degraded fetch returned no root comment id): leave
            # open rather than fire a doomed null-id mutation, same as a missing tid.
            print(f"no comment id for thread {tid}; skipping stamp", file=sys.stderr)
            continue
        new_body = append_stamp(note["body"], note["stamp"])
        if new_body is None:
            # Already stamped (re-run): skip the edit, still resolve.
            stamped_threads.append(tid)
            continue
        urc, uerr = resolution.update_review_comment(note["comment_id"], new_body)
        if urc == 0:
            stamped_threads.append(tid)
        else:
            print(f"comment stamp update failed for thread {tid}: {uerr.strip()}", file=sys.stderr)

    resolved_threads = []
    for tid in stamped_threads + [r["thread_id"] for r in retry]:
        rrc, rerr = resolution.resolve_thread(tid)
        if rrc == 0:
            resolved_threads.append(tid)
        else:
            print(f"resolveReviewThread failed for thread {tid}: {rerr.strip()}", file=sys.stderr)

    return {
        "stamped": stamped_threads,
        "resolved": resolved_threads,
        "retried": [r["thread_id"] for r in retry],
        "degraded": len(degraded),
    }


def _cmd_select(args: argparse.Namespace) -> int:
    threads = json.loads(args.threads.read_text())
    diff = IncrementDiff.parse(args.diff.read_text())
    candidates = select_candidates(
        threads, diff, args.operator, all_open=args.all_open, untouched_cap=args.untouched_cap
    )
    print(json.dumps(candidates, ensure_ascii=False))
    return 0


def _cmd_select_retry(args: argparse.Namespace) -> int:
    threads = json.loads(args.threads.read_text())
    print(json.dumps(select_retry_threads(threads, args.operator), ensure_ascii=False))
    return 0


def _cmd_act(args: argparse.Namespace) -> int:
    notes = json.loads(args.notes.read_text()) if args.notes else []
    retry = json.loads(args.retry.read_text()) if args.retry else []

    if args.dry_run:
        postable, degraded = _vet_notes(notes, args.head_owner, args.head_repo, args.head_sha)
        plan = {
            "would_stamp": [{"thread_id": p["thread_id"], "stamp": p["stamp"]} for p in postable],
            "would_resolve": [p["thread_id"] for p in postable] + [r["thread_id"] for r in retry],
            "degraded": degraded,
        }
        print(json.dumps(plan, ensure_ascii=False))
        return 0

    result = post_and_resolve(
        notes,
        retry,
        head_owner=args.head_owner,
        head_repo=args.head_repo,
        head_sha=args.head_sha,
    )
    print(
        f"resolution: {len(result['stamped'])} stamped, {len(result['resolved'])} resolved "
        f"({len(result['retried'])} retried), {result['degraded']} degraded",
        file=sys.stderr,
    )
    return 0


def _cmd_parse_verdict(args: argparse.Namespace) -> int:
    verdict = parse_verdict(args.raw.read_text())
    print(json.dumps(verdict, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_select = sub.add_parser("select", help="emit candidate threads as JSON")
    p_select.add_argument("--threads", type=Path, required=True, help="open-threads JSON array")
    p_select.add_argument(
        "--diff", type=Path, required=True, help="increment diff (LAST_SHA..HEAD)"
    )
    p_select.add_argument("--operator", required=True, help="daemon's gh login")
    p_select.add_argument(
        "--all-open",
        action="store_true",
        help="take every open daemon thread, skipping the diff filter (force-push/rebase)",
    )
    p_select.add_argument(
        "--untouched-cap",
        type=int,
        default=0,
        help="max untouched threads to also judge per tick (#172 broadening; 0 = touched only)",
    )
    p_select.set_defaults(func=_cmd_select)

    p_retry = sub.add_parser(
        "select-retry", help="emit open threads already carrying a resolution stamp"
    )
    p_retry.add_argument("--threads", type=Path, required=True, help="open-threads JSON array")
    p_retry.add_argument("--operator", required=True, help="daemon's gh login")
    p_retry.set_defaults(func=_cmd_select_retry)

    p_act = sub.add_parser("act", help="stamp resolved Findings in place and resolve their threads")
    p_act.add_argument(
        "--notes",
        type=Path,
        help="JSON array of {thread_id, comment_id, finding_body, path, line, "
        "head_line, head_start_line, rationale}",
    )
    p_act.add_argument("--retry", type=Path, help="JSON array of {thread_id} to resolve only")
    p_act.add_argument("--head-owner", default="", help="head repo owner for the blob link")
    p_act.add_argument("--head-repo", default="", help="head repo name for the blob link")
    p_act.add_argument("--head-sha", default="", help="PR HEAD sha the stamp's blob link points at")
    p_act.add_argument(
        "--dry-run", action="store_true", help="print the stamps and resolves, call no gh"
    )
    p_act.set_defaults(func=_cmd_act)

    p_verdict = sub.add_parser("parse-verdict", help="emit {fixed, rationale} from agent output")
    p_verdict.add_argument("raw", type=Path, help="Fix-check agent stdout capture")
    p_verdict.set_defaults(func=_cmd_parse_verdict)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
