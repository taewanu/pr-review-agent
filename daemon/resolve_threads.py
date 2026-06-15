#!/usr/bin/env python3
"""Commit-driven thread resolution: select, judge, then note-and-resolve (#125, ADR 0017).

The review path runs this on each new HEAD SHA to find prior Findings a commit
may have fixed without any Operator reply, post a `_Fixed:_` note, and resolve the
thread. Safe-biased throughout: any uncertainty leaves the thread open, since a
wrongly-closed live Finding is the dangerous failure and a wrongly-left-open fixed
one is recoverable by a hand click (ADR 0017).

Pure selection/format entry points (unit-tested without gh):

  select_candidates: an open, daemon-owned thread, not yet carrying a `_Fixed:_`
    note, whose Finding line this increment's diff touched. The line tested is the
    thread's `originalLine` (its coordinate in the commit the Finding was posted
    against), matched against the OLD side of `git diff LAST_SHA..HEAD`. GitHub's
    GraphQL `line` is null exactly when a thread goes outdated, which is precisely
    when its code changed (the case we must catch), so `line` is unusable here and
    `originalLine` is the only surviving coordinate. An open, un-noted Finding's
    code is untouched since it was posted (a prior increment would have judged it
    otherwise), so `originalLine` stays a valid old-side coordinate across ticks.

  select_retry_threads: an open, daemon-owned thread that already carries a
    `_Fixed:_` note. The note landed but its resolve dropped (rate-limit); this
    re-resolves it with no re-judgment and no second note (ADR 0017 §4). The
    has_fix_note exclusion in select_candidates is what keeps the two disjoint.

  parse_verdict: the Fix-check agent's {fixed, rationale}. Any parse or schema
    failure returns fixed=False, so a malformed judgment leaves the thread open.

  build_fix_note_body: the `_Fixed:_` note text, voice-gated before posting.

The `act` subcommand posts the notes (batched under one COMMENT review, #38) and
resolves both the freshly-noted and the retry threads, reusing create_reply.py's
posting helpers so a note is voice-gated, carries the Provenance tag, and follows
the #106 reply-lead format.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# daemon/ is not a package and this script is run by path, so add its own dir to
# the import path before importing the shared posting helpers and voice rules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import create_reply  # noqa: E402
import voice  # noqa: E402

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

# Italic colon-lead of the commit-driven note (#106 format, ADR 0017 §3). A new
# lead distinct from the four reply Verdicts, which all answer an Operator reply;
# this one answers a commit and has none.
FIX_NOTE_LEAD = "_Fixed:_"

# Hidden dedup marker on the `_Fixed:_` note (ADR 0017 §4). Distinct from the
# Reply sentinel (create_reply.SENTINEL): that keys on an Operator reply comment
# id a commit-driven note has none of, and #39 reply-detection scans its
# namespace. A thread carrying this marker is past judgment: select_candidates
# skips it (no second note) and select_retry_threads re-resolves it (resolve
# only). lib.sh's fetch_open_review_threads pins this same literal to compute
# `has_fix_note`; test_resolve_threads pins the two identical.
FIX_NOTE_SENTINEL = "<!-- pr-review-agent:fixed -->"


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
      - not yet noted (no `_Fixed:_` note), so a thread judged fixed on an earlier
        tick is never re-judged or re-noted; it goes to select_retry_threads,
      - touched: its `original_line` (creation-side coordinate) falls inside an
        old-side hunk of this increment's diff.

    `all_open` drops the touched test and takes every open, daemon-owned thread.
    The review path sets it on a force-push or rebase, where the increment diff
    can't be computed and the Findings' creation-side lines no longer share a
    coordinate space with the full PR diff (ADR 0017 §1's "full PR diff" scope).
    The has_fix_note exclusion holds there too, so all_open never re-notes a thread.
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
        if t.get("has_fix_note"):
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
    """Open, daemon-owned threads already carrying a `_Fixed:_` note (ADR 0017 §4).

    These are the stuck-open state: the note landed on an earlier tick but the
    resolve mutation dropped under rate-limit. They re-resolve here with no
    re-judgment and no second note; the note is the work, the resolve is the
    retry. Disjoint from select_candidates by construction: a noted thread is
    excluded there (has_fix_note) and selected here. No diff filter, since after a
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
        if not t.get("has_fix_note"):
            continue
        retry.append({"thread_id": t["thread_id"]})
    return retry


def build_fix_note_body(rationale: str, link: str | None) -> str:
    """Assemble the `_Fixed:_` note body (ADR 0017 §3, #106 layout): italic
    colon-lead, the blob-at-HEAD link on the lead line, the one-line rationale
    below, then the Provenance marker and the fix-note sentinel footer.

    Location lives in the link, so the rationale never repeats the file and line.
    `link` is None when there is nothing to anchor (no head sha or no line); the
    lead then stands alone. Voice-gating runs on the rationale before this is
    called, not here."""
    head = f"{FIX_NOTE_LEAD} {link}" if link else FIX_NOTE_LEAD
    return "\n\n".join([head, rationale, PROVENANCE_MARKER, FIX_NOTE_SENTINEL])


def fix_review_body(resolved: int) -> str:
    """Build the batched COMMENT review body wrapping the tick's `_Fixed:_` notes
    (#38): the count first, then the Provenance tag, then the hidden
    REPLY_REVIEW_MARKER.

    Reusing the reply path's marker is deliberate: it makes a crashed-pending
    fix-note wrapper discardable by reply-pr.sh's existing stale-wrapper cleanup,
    so a fix-note crash can't wedge the reply path on GitHub's
    one-pending-review-per-viewer limit. Called only with resolved >= 1."""
    noun = "conversation" if resolved == 1 else "conversations"
    summary = f"{resolved} {noun} resolved as fixed by a later commit."
    return f"{summary}\n\n{PROVENANCE_MARKER}\n\n{create_reply.REPLY_REVIEW_MARKER}"


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
) -> tuple[list[tuple[str, str]], list[str]]:
    """Build and voice-gate each note, returning (postable, skipped).

    `postable` is (thread_id, body) for notes that pass; `skipped` is a log line
    per note whose rationale violates the voice rules. A voice failure leaves that
    thread open (safe bias) rather than posting an off-voice note or resolving
    silently. The lexical gate runs on the agent's rationale (not the whole body),
    so the fixed `_Fixed:_` lead and the marker footer never trip it."""
    postable: list[tuple[str, str]] = []
    skipped: list[str] = []
    for n in notes:
        tid = n["thread_id"]
        rationale = (n.get("rationale") or "").strip()
        violations = voice.check_text(
            rationale,
            prefixes=voice.FORBIDDEN_PREFIXES,
            check_bullets=True,
            label=f"fix-note {tid}",
        )
        if not rationale:
            violations.append(f"fix-note {tid} has an empty rationale")
        if violations:
            skipped.append("; ".join(violations))
            continue
        link = create_reply.build_link(
            head_owner, head_repo, head_sha, n.get("path"), n.get("line")
        )
        postable.append((tid, build_fix_note_body(rationale, link)))
    return postable, skipped


def post_and_resolve(
    notes: list[dict],
    retry: list[dict],
    *,
    pr_node_id: str,
    head_owner: str,
    head_repo: str,
    head_sha: str,
) -> dict:
    """Post the `_Fixed:_` notes under one batched COMMENT review (#38), then
    resolve every thread that now carries a note plus every retry thread.

    Note-then-resolve order and best-effort throughout (ADR 0017 §4): the note is
    the audit trace and must land first, so a thread is only resolved after its
    note is live (the review submitted). A create/add/submit failure leaves the
    thread open and unnoted, so a later tick retries it; on submit failure the
    wrapper is self-deleted so it never lingers to block GitHub's one-pending rule.
    Resolve is idempotent, so a retry thread re-resolves harmlessly.

    The create/add/submit/resolve ladder mirrors create_reply.py's reply batch; it
    reuses the leaf helpers but not the flow. Extracting a shared batch-poster is
    deferred to the #38 notification-batching consolidation (#125), where both call
    sites are in scope; until then a change to the batch protocol must touch both."""
    postable, skipped = _vet_notes(notes, head_owner, head_repo, head_sha)
    for line in skipped:
        print(f"fix-note voice violation, leaving open: {line}", file=sys.stderr)

    noted_threads: list[str] = []
    if postable:
        if not pr_node_id:
            print("no PR node id; cannot batch fix-notes, leaving threads open", file=sys.stderr)
        else:
            _rc, review_id, cerr = create_reply.create_pending_review(pr_node_id)
            if not review_id:
                print(
                    f"fix-note review create failed; leaving {len(postable)} thread(s) open: "
                    f"{cerr.strip()}",
                    file=sys.stderr,
                )
            else:
                added: list[str] = []
                for tid, body in postable:
                    arc, aerr = create_reply.add_thread_reply(review_id, tid, body)
                    if arc == 0:
                        added.append(tid)
                    else:
                        print(
                            f"fix-note add failed for thread {tid}: {aerr.strip()}", file=sys.stderr
                        )
                if not added:
                    create_reply.delete_pending_review(review_id)
                    print("no fix-notes added; discarded the empty review", file=sys.stderr)
                else:
                    src, serr = create_reply.submit_review(review_id, fix_review_body(len(added)))
                    if src == 0:
                        noted_threads = added
                    else:
                        create_reply.delete_pending_review(review_id)
                        print(
                            f"fix-note review submit failed; leaving {len(added)} thread(s) open: "
                            f"{serr.strip()}",
                            file=sys.stderr,
                        )

    resolved_threads = []
    for tid in noted_threads + [r["thread_id"] for r in retry]:
        rrc, rerr = create_reply.resolve_thread(tid)
        if rrc == 0:
            resolved_threads.append(tid)
        else:
            print(f"resolveReviewThread failed for thread {tid}: {rerr.strip()}", file=sys.stderr)

    return {
        "noted": noted_threads,
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
            "would_note": [{"thread_id": tid, "body": body} for tid, body in postable],
            "would_resolve": [tid for tid, _ in postable] + [r["thread_id"] for r in retry],
            "skipped": skipped,
        }
        print(json.dumps(plan, ensure_ascii=False))
        return 0

    result = post_and_resolve(
        notes,
        retry,
        pr_node_id=args.pr_node_id,
        head_owner=args.head_owner,
        head_repo=args.head_repo,
        head_sha=args.head_sha,
    )
    print(
        f"resolution: {len(result['noted'])} noted, {len(result['resolved'])} resolved "
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

    p_act = sub.add_parser("act", help="post fix-notes (batched) and resolve noted + retry threads")
    p_act.add_argument(
        "--notes", type=Path, help="JSON array of {thread_id, path, line, rationale}"
    )
    p_act.add_argument("--retry", type=Path, help="JSON array of {thread_id} to resolve only")
    p_act.add_argument("--pr-node-id", default="", help="PR GraphQL node id for the batched review")
    p_act.add_argument("--head-owner", default="", help="head repo owner for the blob link")
    p_act.add_argument("--head-repo", default="", help="head repo name for the blob link")
    p_act.add_argument("--head-sha", default="", help="PR HEAD sha the note's blob link points at")
    p_act.add_argument(
        "--dry-run", action="store_true", help="print the note bodies and resolves, call no gh"
    )
    p_act.set_defaults(func=_cmd_act)

    p_verdict = sub.add_parser("parse-verdict", help="emit {fixed, rationale} from agent output")
    p_verdict.add_argument("raw", type=Path, help="Fix-check agent stdout capture")
    p_verdict.set_defaults(func=_cmd_parse_verdict)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
