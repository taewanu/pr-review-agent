"""Single-process reply poster (#36).

reply-pr.sh used to build reply bodies through bash -> jq -> `while read -r` ->
gh: three layers where a missing `read -r` or a stray `jq -r` silently mangles
`\\n` / `\\t` / `\\` / backticked-regex payloads. This builds the JSON in one
process and hands it to `gh --input -`, so the body bytes stay intact end to
end; tests/test_create_reply.py pins the round-trip.

Each processed thread also gets an Ack reaction on the operator's reply
comment, chosen by the agent's classification bucket: fix-claims and questions
read as "seen / verifying" (eyes), acknowledgments read as "noted, no action"
(+1). The reaction POST is idempotent on GitHub (200 if the same user+content
reaction already exists, 201 if newly created), so re-running a polling cycle
cannot double-react and we skip a dedup GET entirely.

An acknowledgment carries no text reply, so its dedup marker is the Reply
sentinel embedded in the parent Finding's comment body via a silent PATCH, gated
on the Ack reaction landing first: the reaction is that thread's only ack, so it
must win before we mark the reply processed. fix_claim and answered questions
(#74) keep their sentinel in the text reply. Parent Finding bodies come from
--threads (the same JSON the reply agent consumed), so the PATCH needs no GET.

Body-bearing replies post under ONE pending COMMENTED review per tick (#38)
instead of N detached `/replies` comments, so the operator gets a single GitHub
notification. The body now travels via GraphQL `-f body=<raw>` rather than REST
`--input -`, but gh JSON-encodes the variable, so the bytes still survive intact
(the round-trip test pins both transports). A reply with no thread id (degraded
reviewThreads read, or a thread past the first-100 page) falls back to the
detached REST `/replies` POST so it still lands. The wrapper review body is a
single hidden marker (renders empty, and lets a stale wrapper be told apart from
a Finding-bearing draft before deletion); a visible summary is deferred to #11.

A settled verdict (`confirmed` / `withdrawn`) also resolves its GitHub review
thread via GraphQL after the review submits (#75), using the thread id
reply-pr.sh joined into --threads. Best-effort: a failed resolve is logged,
never retried.

On failure, stderr carries a `category=<x>` line (no-fence, parse-error,
schema-invalid) so reply-pr.sh's log_failure mapping is unchanged.

Usage:
  python3 create_reply.py --owner O --repo R --number N --raw RAWFILE \
    [--threads THREADSFILE] [--pr-node-id NODEID] \
    [--existing-pending-review-id ID] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# daemon/ is not a package and this script is run by path, so add its own dir to
# the import path before importing the shared voice rules (ADR 0010).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_review  # noqa: E402
import voice  # noqa: E402

REQUIRED = ("in_reply_to_id", "addressed_comment_id", "bucket")

# Classification buckets. fix_claim and question carry a text reply (mode +
# body, #74); acknowledgment is a reaction-only ack. The per-bucket mode
# value-set lives in BUCKET_MODES; its first value is the "holds" default the
# agent gets when it omits `mode` (fix_claim → confirmed #37, question → stands
# #74). acknowledgment has no mode.
VALID_BUCKETS = ("fix_claim", "question", "acknowledgment")
BUCKET_MODES = {
    "fix_claim": ("confirmed", "pushback"),
    "question": ("stands", "withdrawn"),
}

# Verdicts that leave nothing actionable on the thread, so the daemon resolves
# the GitHub review thread after the reply lands (#75): `confirmed` (the fix
# landed) and `withdrawn` (the Finding was retracted as a false positive).
# `pushback` and `stands` keep the Finding live; `acknowledgment` carries no
# verdict — all three leave the thread open. Resolution is a user-facing GitHub
# state change, orthogonal to the Reply sentinel (CONTEXT.md "Thread resolution").
RESOLVE_MODES = ("confirmed", "withdrawn")

# Resolution-stamp rationale per resolving Verdict (ADR 0019). A reply-driven
# resolution names no commit (unlike the commit-driven stamp), so the rationale
# records who and why: a confirmed fix vs a retracted false positive. Hard-coded and
# clean, so unlike the agent's commit-driven rationale it needs no voice gate.
STAMP_RATIONALE = {
    "confirmed": "confirmed fixed by the author",
    "withdrawn": "withdrawn by the author as a false positive",
}

# The batched-review GraphQL leaves, the wrapper marker, and the blob-link helper
# now live in batch_review (#125), shared with resolve_threads so neither imports
# the other. REPLY_REVIEW_MARKER kept here as the path-local name for the wrapper
# marker's one producer (this module's reply acks); the wire string is identical.
REPLY_REVIEW_MARKER = batch_review.WRAPPER_MARKER

# Ack reaction per bucket. GitHub's reaction set is fixed to
# +1/-1/laugh/confused/heart/hooray/rocket/eyes, so the design note's 🙏 is not
# postable; +1 is the "noted" marker. fix_claim/question read as "seen".
BUCKET_REACTION = {
    "fix_claim": "eyes",
    "question": "eyes",
    "acknowledgment": "+1",
}

# Reply sentinel. `{id}` is the operator reply's comment id, so the next polling
# cycle's detection (#39) skips that reply. Carried in a body-bearing reply
# (fix_claim or answered question) or, for an acknowledgment, in the parent
# Finding's comment body (#79).
SENTINEL = "<!-- pr-review-agent:reply:{id} -->"

# Provenance marker appended to every daemon text reply. Answers "who wrote
# this" under the shared solo identity (ADR 0003); never draft-status, since a
# posted reply is no draft (ADR 0010 §1). Hard-coded per ADR 0010 §3 (a runtime
# shared constant across the bash/Python boundary was rejected there); mirrors
# lib.sh's PROVENANCE_TAG, with test_provenance_tag.py pinning the two identical.
# The trailing `_` closes the markdown italic; it carries no colon, so the
# sentinel scan in reply-pr.sh never false-matches it.
MARKER = "🤖 _pr-review-agent_"


class PayloadError(Exception):
    """Carries a log_failure category so reply-pr.sh can classify the exit."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def extract_payload(raw: str) -> dict:
    """Parse the reply agent's stdout into a validated {"replies": [...]} dict.

    Same fence convention as extract_json.py: the last ```json block wins.
    Every thread the agent processed appears here tagged with its `bucket`.
    `mode` and `body` are required for the body-bearing buckets (fix_claim and
    question, #74); `mode`'s value-set is per-bucket (BUCKET_MODES) and defaults
    to that bucket's "holds" verdict. An empty body is rejected: under the "has
    body" poster discriminator it would silently post nothing.
    """
    matches = re.findall(r"```json\s*\n(.*?)\n```", raw, re.DOTALL)
    if not matches:
        raise PayloadError("no-fence", "reply agent: no ```json fence in output")
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        raise PayloadError("parse-error", f"reply agent: JSON decode failed: {exc}") from exc

    if "replies" not in data or not isinstance(data["replies"], list):
        raise PayloadError("schema-invalid", "reply agent: missing or non-list 'replies' key")

    # Voice violations are collected across all body-bearing replies and raised
    # once after the loop, so the whole batch fails before any POST (ADR 0010),
    # symmetric with extract_json.py. Schema errors above still abort eagerly and
    # take precedence: a malformed payload never reaches the voice gate.
    style_violations: list[str] = []
    for i, r in enumerate(data["replies"]):
        missing = [k for k in REQUIRED if k not in r]
        if missing:
            raise PayloadError(
                "schema-invalid", f"reply agent: replies[{i}] missing keys: {missing}"
            )
        bucket = r["bucket"]
        if bucket not in VALID_BUCKETS:
            raise PayloadError(
                "schema-invalid",
                f"reply agent: replies[{i}] bucket {bucket!r} not in {VALID_BUCKETS}",
            )
        if bucket in BUCKET_MODES:
            body = r.get("body")
            if not (isinstance(body, str) and body.strip()):
                raise PayloadError(
                    "schema-invalid",
                    f"reply agent: replies[{i}] {bucket} missing or empty 'body'",
                )
            valid = BUCKET_MODES[bucket]
            mode = r.setdefault("mode", valid[0])
            if mode not in valid:
                raise PayloadError(
                    "schema-invalid",
                    f"reply agent: replies[{i}] {bucket} mode {mode!r} not in {valid}",
                )
            # Reply bodies lead with an italic sentence (diverging from the
            # inline comment's bold lead, ADR 0010 §4), peeled before the opener
            # scan (strip_bold handles `_…_` too since #104) and held to the
            # same 2–4 bullet rule (check_bullets).
            style_violations += voice.check_text(
                body,
                prefixes=voice.FORBIDDEN_PREFIXES,
                strip_bold=True,
                check_bullets=True,
                label=f"replies[{i}].body",
            )

    if style_violations:
        raise PayloadError("style-violation", "reply agent: " + "; ".join(style_violations))
    return data


def link_for(args: argparse.Namespace, reply: dict) -> str | None:
    """The blob-at-HEAD link for one body-bearing reply, from its `verified_*`
    fields plus the head repo and head sha. None when nothing to anchor. The link
    targets the head repo (where `head_sha` lives), falling back to the base
    owner/repo when not supplied so same-repo PRs and older callers still link."""
    return batch_review.build_blob_link(
        args.head_owner or args.owner,
        args.head_repo or args.repo,
        args.head_sha,
        reply.get("verified_path"),
        reply.get("verified_line"),
        reply.get("verified_end_line"),
    )


def build_body(body: str, addressed_id: str, link: str | None = None) -> str:
    """Verdict-leading reply body: the italic lead sentence, the optional
    blob-at-HEAD link on the same line, the explanation prose below it, then the
    provenance marker and Reply sentinel footer. Location lives in the link, not
    the prose, so the agent body never repeats the file and line (#96). When the
    body has no italic lead (degenerate — replies are validated to lead with one),
    it stays whole with the link as a trailing paragraph, the pre-#96 layout."""
    lead, rest = voice.split_lead(body)
    if lead:
        parts = [f"{lead} {link}" if link else lead]
        if rest:
            parts.append(rest)
    else:
        parts = [body]
        if link:
            parts.append(link)
    parts.append(MARKER)
    parts.append(SENTINEL.format(id=addressed_id))
    return "\n\n".join(parts)


def create_reply(
    owner: str, repo: str, number: str, in_reply_to_id: str, body: str
) -> tuple[int, str]:
    """POST one threaded reply. /comments/{id}/replies inherits path+line from
    the parent, so body is the only field. The payload is built with json.dumps
    and fed on stdin via `--input -` — no shell-quoting of the body anywhere."""
    payload = json.dumps({"body": body})
    endpoint = f"repos/{owner}/{repo}/pulls/{number}/comments/{in_reply_to_id}/replies"
    proc = subprocess.run(
        ["gh", "api", "--method", "POST", endpoint, "--input", "-"],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stderr


def post_reaction(owner: str, repo: str, comment_id: str, content: str) -> tuple[int, str]:
    """POST an Ack reaction on the operator's reply comment. Idempotent on
    GitHub's side (keyed on user+content), so a blind POST every cycle is safe
    and needs no GET-reactions dedup."""
    payload = json.dumps({"content": content})
    endpoint = f"repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions"
    proc = subprocess.run(
        ["gh", "api", "--method", "POST", endpoint, "--input", "-"],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stderr


def patch_finding(owner: str, repo: str, finding_id: str, body: str) -> tuple[int, str]:
    """PATCH a Finding's Inline comment body to embed the Reply sentinel for a
    non-claim thread. The sentinel rides in a comment we own (the parent
    Finding), invisible in rendered markdown, so the next polling cycle's
    detection skips the reply. Same idiom as lib.sh's status-comment edit; body
    is the full replacement, so callers pass the captured body plus footer."""
    payload = json.dumps({"body": body})
    endpoint = f"repos/{owner}/{repo}/pulls/comments/{finding_id}"
    proc = subprocess.run(
        ["gh", "api", "--method", "PATCH", endpoint, "--input", "-"],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stderr


def disposition_summary(open_count: int, resolved: int) -> str:
    """One-line disposition rollup for the Reply review body (#11).

    Leads with the conversations still needing the author (open: pushback +
    stands), then the resolved ones (confirmed + withdrawn). "conversation" is
    GitHub's user-facing name for a review thread (the "Resolve conversation"
    control); the API-layer "thread" stays in the code. Built deterministically
    so the count never drifts and the line clears the voice summary rules by
    construction. Called only with at least one landed reply, so open + resolved
    is never zero."""

    def conversations(n: int) -> str:
        return "1 conversation" if n == 1 else f"{n} conversations"

    if open_count and resolved:
        return f"{conversations(open_count)} still open, {resolved} resolved."
    if open_count:
        return f"{conversations(open_count)} still open."
    if resolved == 1:
        return "1 conversation resolved."
    return f"All {conversations(resolved)} resolved."


def reply_review_body(open_count: int, resolved: int) -> str:
    """The Reply review's COMMENT body: the disposition summary, the Provenance
    tag, then the hidden reply-review marker. The Reply review body is a posted
    artifact, not a Finding Review body, so it carries the tag like every other
    (ADR 0010 §2, #132). The marker stays last so the daemon can tell its own
    stale wrapper from a Finding draft before deleting one."""
    return f"{disposition_summary(open_count, resolved)}\n\n{MARKER}\n\n{REPLY_REVIEW_MARKER}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Post threaded reply acks for reply-pr.sh (#36).")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--number", required=True)
    parser.add_argument("--raw", required=True, help="reply agent stdout capture")
    parser.add_argument(
        "--head-sha",
        default="",
        help="PR HEAD sha; the blob-at-HEAD reply link is built against it (#11)",
    )
    parser.add_argument(
        "--head-owner",
        default="",
        help="head repo owner for the blob link (the fork on a cross-repo PR); defaults to --owner",
    )
    parser.add_argument(
        "--head-repo",
        default="",
        help="head repo name for the blob link; defaults to --repo",
    )
    parser.add_argument(
        "--threads",
        help="threads JSON the reply agent consumed; supplies parent Finding "
        "bodies for the non-claim Reply-sentinel PATCH",
    )
    parser.add_argument(
        "--pr-node-id",
        default="",
        help="PR GraphQL node id; body-bearing replies batch under one pending "
        "COMMENTED review against it (#38). Empty -> every reply falls back to a "
        "detached REST reply (the pre-#38 path), so a degraded reviewThreads read "
        "still posts.",
    )
    parser.add_argument(
        "--existing-pending-review-id",
        default="",
        help="a viewer pending review already on the PR (a prior tick that failed "
        "to submit); deleted before opening this tick's review so the create is not "
        "blocked and orphan comments do not re-trigger duplicate replies (#38)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the actions that would be posted, do not call gh",
    )
    args = parser.parse_args()

    raw = Path(args.raw).read_text()
    try:
        data = extract_payload(raw)
    except PayloadError as exc:
        print(f"category={exc.category}", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 1

    # Parent Finding bodies keyed by comment id (== a reply's in_reply_to_id),
    # so a non-claim PATCH appends the sentinel to the captured body with no GET.
    # thread_ids carries the GraphQL review-thread node id reply-pr.sh joined in
    # (#75), keyed the same way, so a settled verdict resolves its thread. A
    # thread whose id could not be mapped (degraded reviewThreads query) is
    # absent here and skips resolution.
    finding_bodies: dict[str, str] = {}
    thread_ids: dict[str, str] = {}
    if args.threads:
        for t in json.loads(Path(args.threads).read_text()):
            pf = t.get("parent_finding") or {}
            cid = pf.get("comment_id")
            if cid is not None:
                finding_bodies[str(cid)] = pf.get("body") or ""
                if t.get("thread_id"):
                    thread_ids[str(cid)] = t["thread_id"]

    replies = data["replies"]
    fix = sum(1 for r in replies if r["bucket"] == "fix_claim")
    question = sum(1 for r in replies if r["bucket"] == "question")
    ack = sum(1 for r in replies if r["bucket"] == "acknowledgment")
    print(
        f"{len(replies)} thread(s): {fix} fix-claim, {question} question, {ack} acknowledgment",
        file=sys.stderr,
    )
    if not replies:
        return 0

    if args.dry_run:
        plan = []
        for r in replies:
            bucket = r["bucket"]
            addressed_id = str(r["addressed_comment_id"])
            in_reply_to_id = str(r["in_reply_to_id"])
            entry = {
                "addressed_comment_id": addressed_id,
                "bucket": bucket,
                "reaction": BUCKET_REACTION[bucket],
            }
            if r.get("body"):
                entry["in_reply_to_id"] = in_reply_to_id
                # "review" -> wrapped in the one pending COMMENTED review (#38);
                # "detached" -> a standalone REST reply (degraded read / no thread id).
                entry["via"] = (
                    "review" if args.pr_node_id and thread_ids.get(in_reply_to_id) else "detached"
                )
                entry["reply_payload"] = {
                    "body": build_body(r["body"], addressed_id, link_for(args, r))
                }
                if r.get("mode") in RESOLVE_MODES:
                    entry["resolve_thread_id"] = thread_ids.get(in_reply_to_id)
                    entry["resolution_stamp"] = batch_review.build_resolution_stamp(
                        STAMP_RATIONALE[r["mode"]], None
                    )
            else:
                entry["sentinel_patch"] = {
                    "finding_id": in_reply_to_id,
                    "sentinel": SENTINEL.format(id=addressed_id),
                }
            plan.append(entry)
        print(json.dumps({"plan": plan}, ensure_ascii=False))
        return 0

    text_total = fix + question  # body-bearing buckets post a text reply
    sentinel_total = ack  # only acknowledgment dedups via a parent-Finding PATCH
    resolve_total = sum(1 for r in replies if r.get("mode") in RESOLVE_MODES)
    text_ok = 0
    react_ok = 0
    patch_ok = 0
    resolve_ok = 0
    stamp_ok = 0
    # A running copy of each Finding body, so accumulating edits (the resolution
    # stamp here, the acknowledgment Reply sentinel later) build on each other
    # instead of clobbering.
    finding_work = dict(finding_bodies)

    def do_resolve(reply: dict) -> None:
        """Stamp the settled Finding's comment resolved, then resolve its thread (#75,
        ADR 0019). Stamp-then-resolve matches the commit-driven order, so the trace
        lands before the thread collapses; both are best-effort and independent (a
        missing parent body skips the stamp, a missing thread id skips the resolve).
        The resolve is not gated on the stamp: the threaded ack already records the
        outcome, and a stamped-but-open thread is re-resolved by the commit path's
        retry."""
        nonlocal resolve_ok, stamp_ok
        fid = str(reply["in_reply_to_id"])
        base = finding_work.get(fid)
        if base is None:
            print(f"no parent body for finding {fid}; skipping stamp", file=sys.stderr)
        else:
            stamp = batch_review.build_resolution_stamp(STAMP_RATIONALE[reply["mode"]], None)
            new_body = batch_review.append_stamp(base, stamp)
            if new_body is not None:  # None -> already stamped (a re-run): no second edit
                rc, err = patch_finding(args.owner, args.repo, fid, new_body)
                if rc == 0:
                    finding_work[fid] = new_body
                    stamp_ok += 1
                else:
                    print(
                        f"resolution-stamp PATCH failed for finding {fid}: {err.strip()}",
                        file=sys.stderr,
                    )
        tid = thread_ids.get(fid)
        if tid:
            rrc, rerr = batch_review.resolve_thread(tid)
            if rrc == 0:
                resolve_ok += 1
            else:
                print(
                    f"resolveReviewThread failed for thread {tid}: {rerr.strip()}", file=sys.stderr
                )
        else:
            print(f"no thread id for finding {fid}; skipping resolve", file=sys.stderr)

    # Partition body-bearing replies: batch under one pending review when we have
    # a PR node id AND the reply's thread id (#38); otherwise fall back to a
    # detached REST reply — a degraded reviewThreads read (no pr node id) or a
    # thread past the first-100 page (no thread id). acknowledgments carry no body
    # and post no text reply, so they sit out the batch entirely.
    batched: list[dict] = []
    fallback: list[dict] = []
    for r in replies:
        if not r.get("body"):
            continue
        if args.pr_node_id and thread_ids.get(str(r["in_reply_to_id"])):
            batched.append(r)
        else:
            fallback.append(r)

    # --- Batch: one COMMENTED review (empty body) wraps every batchable reply via
    # batch_review.submit, so the operator gets a single notification for the tick
    # (#38). A failed add or submit lands no reply (and no sentinel) on the affected
    # thread, so the next cycle retries it — the poster's best-effort contract.
    if batched:
        items = [
            batch_review.BatchItem(
                thread_ids[str(r["in_reply_to_id"])],
                build_body(r["body"], str(r["addressed_comment_id"]), link_for(args, r)),
                tag=r,
            )
            for r in batched
        ]

        def reply_wrapper_body(landed: list[batch_review.BatchItem]) -> str:
            resolved = sum(1 for it in landed if it.tag.get("mode") in RESOLVE_MODES)
            return reply_review_body(len(landed) - resolved, resolved)

        added = batch_review.submit(
            items,
            pr_node_id=args.pr_node_id,
            review_body=reply_wrapper_body,
            existing_review_id=args.existing_pending_review_id,
        )
        text_ok += len(added)
        # Resolve only after submit — replies (and their sentinels) are not live
        # until the review is submitted.
        for it in added:
            if it.tag.get("mode") in RESOLVE_MODES:
                do_resolve(it.tag)

    # --- Fallback: a detached REST reply for any non-batchable body reply. Same
    # path the daemon used before #38; resolution stays inline since these post
    # immediately (no pending-review barrier).
    for r in fallback:
        in_reply_to_id = str(r["in_reply_to_id"])
        full_body = build_body(r["body"], str(r["addressed_comment_id"]), link_for(args, r))
        rc, err = create_reply(args.owner, args.repo, args.number, in_reply_to_id, full_body)
        if rc == 0:
            text_ok += 1
            if r.get("mode") in RESOLVE_MODES:
                do_resolve(r)
        else:
            print(f"reply POST failed for comment {in_reply_to_id}: {err.strip()}", file=sys.stderr)

    # --- Reactions + bodiless sentinel PATCH: every reply, independent of the
    # text-reply path above. The reaction is idempotent; an acknowledgment then
    # embeds the Reply sentinel in its parent Finding once the reaction lands (its
    # only dedup carrier). finding_work (above) carries any stamp already written, so
    # a sentinel PATCH builds on it instead of clobbering.
    for r in replies:
        bucket = r["bucket"]
        addressed_id = str(r["addressed_comment_id"])
        in_reply_to_id = str(r["in_reply_to_id"])
        has_body = bool(r.get("body"))

        content = BUCKET_REACTION[bucket]
        rc, err = post_reaction(args.owner, args.repo, addressed_id, content)
        if rc != 0:
            print(
                f"reaction POST failed for comment {addressed_id}: {err.strip()}",
                file=sys.stderr,
            )
            # The reaction is a bodiless thread's only ack, so skip the sentinel
            # PATCH and let the next cycle retry the reaction first.
            continue
        react_ok += 1

        if not has_body:
            base = finding_work.get(in_reply_to_id)
            if base is None:
                print(
                    f"no parent body for finding {in_reply_to_id}; "
                    f"skipping sentinel PATCH for reply {addressed_id}",
                    file=sys.stderr,
                )
                continue
            new_body = f"{base}\n\n{SENTINEL.format(id=addressed_id)}"
            rc, err = patch_finding(args.owner, args.repo, in_reply_to_id, new_body)
            if rc == 0:
                finding_work[in_reply_to_id] = new_body
                patch_ok += 1
            else:
                print(
                    f"sentinel PATCH failed for finding {in_reply_to_id}: {err.strip()}",
                    file=sys.stderr,
                )

    summary = f"done — {text_ok}/{text_total} replies, {react_ok}/{len(replies)} reactions"
    if sentinel_total:
        summary += f", {patch_ok}/{sentinel_total} sentinels"
    if resolve_total:
        summary += f", {resolve_ok}/{resolve_total} threads resolved, {stamp_ok} stamped"
    print(summary + " posted", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
