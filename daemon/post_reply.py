"""Single-process reply poster (#36).

reply-pr.sh used to build reply bodies through bash -> jq -> `while read -r` ->
gh: three layers where a missing `read -r` or a stray `jq -r` silently mangles
`\\n` / `\\t` / `\\` / backticked-regex payloads. This builds the JSON in one
process and hands it to `gh --input -`, so the body bytes stay intact end to
end; tests/test_post_reply.py pins the round-trip.

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

On failure, stderr carries a `category=<x>` line (no-fence, parse-error,
schema-invalid) so reply-pr.sh's log_failure mapping is unchanged.

Usage:
  python3 post_reply.py --owner O --repo R --number N --raw RAWFILE \
    [--threads THREADSFILE] [--dry-run]
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

# Provenance marker appended to every daemon text reply (#11). Answers "who
# wrote this" under the shared solo identity (ADR 0003). Not "drafted": a posted
# reply is never a draft. The trailing `_` closes the markdown italic; it carries
# no colon, so the sentinel scan in reply-pr.sh never false-matches it.
MARKER = "🤖 _pr-review-agent_"


class PayloadError(Exception):
    """Carries a log_failure category so reply-pr.sh can classify the exit."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def extract_payload(raw: str) -> dict:
    """Parse the reply agent's stdout into a validated {"replies": [...]} dict.

    Same fence convention as extract-json.py: the last ```json block wins.
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
    # symmetric with extract-json.py. Schema errors above still abort eagerly and
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
            # Reply bodies lead with a bold sentence like Inline comments, so
            # peel it before the opener scan (strip_bold).
            style_violations += voice.check_text(
                body,
                prefixes=voice.FORBIDDEN_PREFIXES,
                strip_bold=True,
                label=f"replies[{i}].body",
            )

    if style_violations:
        raise PayloadError("style-violation", "reply agent: " + "; ".join(style_violations))
    return data


def build_link(
    owner: str,
    repo: str,
    head_sha: str,
    path: str | None,
    line: object,
    end_line: object = None,
) -> str | None:
    """A blob-at-HEAD deep link to the verified line(s), or None when there is
    nothing to anchor (no head sha, or the agent emitted no line, for example a
    confirmed-by-deletion).

    `owner`/`repo` must be the **head** repo: `head_sha` is a commit in the fork,
    so a base-repo blob URL 404s on a cross-repo PR. The daemon reply asserts the
    file's *current* state, so it points at the blob at HEAD (#11), not a
    per-commit diff. The anchor is GitHub's plain `#L<n>` blob form; the sha256
    path hash is the per-commit *diff* anchor and does not apply here. The label
    shows a short sha plus line so the destination reads without hovering; the URL
    carries the full sha for stability."""
    if not (head_sha and path and line):
        return None
    try:
        start = int(line)
        end = int(end_line) if end_line else None
    except (TypeError, ValueError):
        return None
    if end and end != start:
        frag = f"L{start}-L{end}"
        label = f"{head_sha[:7]}:L{start}-L{end}"
    else:
        frag = f"L{start}"
        label = f"{head_sha[:7]}:L{start}"
    url = f"https://github.com/{owner}/{repo}/blob/{head_sha}/{path}#{frag}"
    return f"[`{label}`]({url})"


def link_for(args: argparse.Namespace, reply: dict) -> str | None:
    """The blob-at-HEAD link for one body-bearing reply, from its `verified_*`
    fields plus the head repo and head sha. None when nothing to anchor. The link
    targets the head repo (where `head_sha` lives), falling back to the base
    owner/repo when not supplied so same-repo PRs and older callers still link."""
    return build_link(
        args.head_owner or args.owner,
        args.head_repo or args.repo,
        args.head_sha,
        reply.get("verified_path"),
        reply.get("verified_line"),
        reply.get("verified_end_line"),
    )


def build_body(body: str, addressed_id: str, link: str | None = None) -> str:
    """Agent body, the optional blob-at-HEAD link, the provenance marker, and the
    Reply sentinel footer, joined byte-for-byte. Location lives in the link, not
    the prose, so the agent body never repeats the file and line."""
    parts = [body]
    if link:
        parts.append(link)
    parts.append(MARKER)
    parts.append(SENTINEL.format(id=addressed_id))
    return "\n\n".join(parts)


def post_reply(
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
    finding_bodies: dict[str, str] = {}
    if args.threads:
        for t in json.loads(Path(args.threads).read_text()):
            pf = t.get("parent_finding") or {}
            cid = pf.get("comment_id")
            if cid is not None:
                finding_bodies[str(cid)] = pf.get("body") or ""

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
                entry["reply_payload"] = {
                    "body": build_body(r["body"], addressed_id, link_for(args, r))
                }
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
    text_ok = 0
    react_ok = 0
    patch_ok = 0
    # Running copy of each Finding body so two non-claim replies on the same
    # Finding in one cycle accumulate both sentinels instead of clobbering: the
    # second PATCH appends to the body the first one already extended.
    finding_work = dict(finding_bodies)

    for r in replies:
        bucket = r["bucket"]
        addressed_id = str(r["addressed_comment_id"])
        in_reply_to_id = str(r["in_reply_to_id"])
        has_body = bool(r.get("body"))

        # Text reply: any body-bearing bucket (fix_claim + answered question,
        # #74). A failed POST leaves no sentinel, so the next polling cycle
        # re-detects and retries this thread (best-effort, matching the prior
        # bash loop).
        if has_body:
            full_body = build_body(r["body"], addressed_id, link_for(args, r))
            rc, err = post_reply(args.owner, args.repo, args.number, in_reply_to_id, full_body)
            if rc == 0:
                text_ok += 1
            else:
                print(
                    f"reply POST failed for comment {in_reply_to_id}: {err.strip()}",
                    file=sys.stderr,
                )

        # Ack reaction: every bucket. Idempotent, so no dedup needed.
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

        # Bodiless dedup: an acknowledgment carries no text reply and no author
        # provenance, so embed the Reply sentinel in the parent Finding (a
        # comment we own) once the reaction lands. Body buckets already carry
        # their sentinel in the reply.
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
    print(summary + " posted", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
