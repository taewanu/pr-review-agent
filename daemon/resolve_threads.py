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

  select_candidates: an open, daemon-owned thread, not yet carrying a resolution
    stamp, whose Finding line this increment's diff touched. The line tested is the
    thread's `originalLine` (its coordinate in the commit the Finding was posted
    against), matched against the OLD side of `git diff LAST_SHA..HEAD`. GitHub's
    GraphQL `line` is null exactly when a thread goes outdated, which is precisely
    when its code changed (the case we must catch), so `line` is unusable here and
    `originalLine` is the only surviving coordinate. An open, un-stamped Finding's
    code is untouched since it was posted (a prior increment would have judged it
    otherwise), so `originalLine` stays a valid old-side coordinate across ticks.

  select_retry_threads: an open, daemon-owned thread that already carries a
    resolution stamp. The stamp landed but its resolve dropped (rate-limit); this
    re-resolves it with no re-judgment and no second stamp (ADR 0017 §4). The
    has_resolution_stamp exclusion in select_candidates is what keeps the two disjoint.

  parse_verdict: the Fix-check agent's {fixed, rationale}. Any parse or schema
    failure returns fixed=False, so a malformed judgment leaves the thread open.

  build_resolution_stamp / append_stamp: the stamp text (voice-gated before the
    edit) and the idempotent append onto the Finding comment's existing body.

The `act` subcommand stamps each resolved Finding's comment in place via
batch_review.update_review_comment and resolves both the freshly-stamped and the
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
# the import path before importing the shared posting toolkit, the blob-link
# helper, and the voice rules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_review  # noqa: E402
import voice  # noqa: E402
from batch_review import (  # noqa: E402
    append_stamp,
    build_blob_link,
    build_resolution_stamp,
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
    threads: list[dict], diff: IncrementDiff, operator: str, all_open: bool = False
) -> list[dict]:
    """Open, daemon-owned threads whose Finding line this increment touched.

    A candidate is the thread of a prior Finding the new HEAD might have fixed:
      - open (not already resolved on GitHub),
      - daemon-owned (root comment authored by the operator AND carrying the
        Provenance marker, so an Operator's own manual comment never qualifies),
      - not yet stamped (no resolution stamp), so a thread judged fixed on an earlier
        tick is never re-judged or re-stamped; it goes to select_retry_threads,
      - touched: its `original_line` (creation-side coordinate) falls inside an
        old-side hunk of this increment's diff.

    `all_open` drops the touched test and takes every open, daemon-owned thread.
    The review path sets it on a force-push or rebase, where the increment diff
    can't be computed and the Findings' creation-side lines no longer share a
    coordinate space with the full PR diff (ADR 0017 §1's "full PR diff" scope).
    The has_resolution_stamp exclusion holds there too, so all_open never re-notes a thread.
    """
    candidates: list[dict] = []
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
        if not all_open and not diff.touched(path, line, start_line):
            continue
        candidates.append(
            {
                "thread_id": t["thread_id"],
                "path": path,
                "line": line,
                "finding_body": body,
            }
        )
    return candidates


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


def _vet_notes(
    notes: list[dict], head_owner: str, head_repo: str, head_sha: str
) -> tuple[list[dict], list[str]]:
    """Build and voice-gate each note's Resolution stamp, returning (postable, skipped).

    `postable` is a dict per note that passes, carrying everything the edit needs:
    `thread_id`, `comment_id` (the Finding's root comment node id, the edit target),
    `body` (its current body, to append the stamp below), and `stamp`. `skipped` is a
    log line per note whose rationale violates the voice rules. A voice failure leaves
    that thread open (safe bias) rather than editing an off-voice stamp or resolving
    silently. The lexical gate runs on the agent's rationale (not the whole stamp), so
    the fixed lead and the sentinel never trip it."""
    postable: list[dict] = []
    skipped: list[str] = []
    for n in notes:
        tid = n["thread_id"]
        rationale = (n.get("rationale") or "").strip()
        violations = voice.check_text(
            rationale,
            prefixes=voice.FORBIDDEN_PREFIXES,
            check_bullets=True,
            label=f"resolution stamp {tid}",
        )
        if not rationale:
            violations.append(f"resolution stamp {tid} has an empty rationale")
        if violations:
            skipped.append("; ".join(violations))
            continue
        link = build_blob_link(head_owner, head_repo, head_sha, n.get("path"), n.get("line"))
        postable.append(
            {
                "thread_id": tid,
                "comment_id": n.get("comment_id"),
                "body": n.get("finding_body") or "",
                "stamp": build_resolution_stamp(rationale, link),
            }
        )
    return postable, skipped


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
    postable, skipped = _vet_notes(notes, head_owner, head_repo, head_sha)
    for line in skipped:
        print(f"resolution-stamp voice violation, leaving open: {line}", file=sys.stderr)

    stamped_threads = []
    for note in postable:
        tid = note["thread_id"]
        new_body = append_stamp(note["body"], note["stamp"])
        if new_body is None:
            # Already stamped (re-run): skip the edit, still resolve.
            stamped_threads.append(tid)
            continue
        urc, uerr = batch_review.update_review_comment(note["comment_id"], new_body)
        if urc == 0:
            stamped_threads.append(tid)
        else:
            print(f"comment stamp update failed for thread {tid}: {uerr.strip()}", file=sys.stderr)

    resolved_threads = []
    for tid in stamped_threads + [r["thread_id"] for r in retry]:
        rrc, rerr = batch_review.resolve_thread(tid)
        if rrc == 0:
            resolved_threads.append(tid)
        else:
            print(f"resolveReviewThread failed for thread {tid}: {rerr.strip()}", file=sys.stderr)

    return {
        "stamped": stamped_threads,
        "resolved": resolved_threads,
        "retried": [r["thread_id"] for r in retry],
        "skipped": len(skipped),
    }


def _cmd_select(args: argparse.Namespace) -> int:
    threads = json.loads(args.threads.read_text())
    diff = IncrementDiff.parse(args.diff.read_text())
    candidates = select_candidates(threads, diff, args.operator, all_open=args.all_open)
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
        postable, skipped = _vet_notes(notes, args.head_owner, args.head_repo, args.head_sha)
        plan = {
            "would_stamp": [{"thread_id": p["thread_id"], "stamp": p["stamp"]} for p in postable],
            "would_resolve": [p["thread_id"] for p in postable] + [r["thread_id"] for r in retry],
            "skipped": skipped,
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
        f"({len(result['retried'])} retried), {result['skipped']} skipped",
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
    p_select.set_defaults(func=_cmd_select)

    p_retry = sub.add_parser("select-retry", help="emit open threads already carrying a fix-note")
    p_retry.add_argument("--threads", type=Path, required=True, help="open-threads JSON array")
    p_retry.add_argument("--operator", required=True, help="daemon's gh login")
    p_retry.set_defaults(func=_cmd_select_retry)

    p_act = sub.add_parser("act", help="stamp resolved Findings in place and resolve their threads")
    p_act.add_argument(
        "--notes",
        type=Path,
        help="JSON array of {thread_id, comment_id, finding_body, path, line, rationale}",
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
