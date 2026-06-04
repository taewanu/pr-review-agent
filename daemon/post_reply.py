"""Single-process reply poster (#36).

reply-pr.sh used to build reply bodies through bash -> jq -> `while read -r` ->
gh: three layers where a missing `read -r` or a stray `jq -r` silently mangles
`\\n` / `\\t` / `\\` / backticked-regex payloads. This builds the JSON in one
process and hands it to `gh --input -`, so the body bytes stay intact end to
end; tests/test_post_reply.py pins the round-trip.

Each processed thread also gets a pickup reaction on the operator's reply
comment, chosen by the agent's classification bucket: fix-claims and questions
read as "seen / verifying" (eyes), acknowledgments read as "noted, no action"
(+1). The reaction POST is idempotent on GitHub (200 if the same user+content
reaction already exists, 201 if newly created), so re-running a polling cycle
cannot double-react and we skip a dedup GET entirely.

On failure, stderr carries a `category=<x>` line (no-fence, parse-error,
schema-invalid) so reply-pr.sh's log_failure mapping is unchanged.

Usage:
  python3 post_reply.py --owner O --repo R --number N --raw RAWFILE [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REQUIRED = ("in_reply_to_id", "addressed_comment_id", "bucket")
VALID_MODES = ("confirmed", "pushback")

# Classification buckets emitted by review-agent-reply (#51). Only fix_claim
# carries a text reply (mode + body); question and acknowledgment are
# reaction-only acks that replace the prior silence.
VALID_BUCKETS = ("fix_claim", "question", "acknowledgment")

# Pickup reaction per bucket. GitHub's reaction set is fixed to
# +1/-1/laugh/confused/heart/hooray/rocket/eyes, so the design note's 🙏 is not
# postable; +1 is the "noted" marker. fix_claim/question read as "seen".
BUCKET_REACTION = {
    "fix_claim": "eyes",
    "question": "eyes",
    "acknowledgment": "+1",
}

# `\n\n` separates the agent's prose from the sentinel; the next polling
# cycle's sentinel-based detection (#39) greps this marker to skip the reply.
SENTINEL = "<!-- pr-review-agent:addressed:{id} -->"


class PayloadError(Exception):
    """Carries a log_failure category so reply-pr.sh can classify the exit."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def extract_payload(raw: str) -> dict:
    """Parse the reply agent's stdout into a validated {"replies": [...]} dict.

    Same fence convention as extract-json.py: the last ```json block wins.
    Every thread the agent processed appears here tagged with its `bucket`;
    `mode` (default `confirmed`, #37) and `body` are required only for
    fix_claim, the one bucket that posts a text reply.
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
        if bucket == "fix_claim":
            if "body" not in r:
                raise PayloadError(
                    "schema-invalid", f"reply agent: replies[{i}] fix_claim missing 'body'"
                )
            mode = r.setdefault("mode", "confirmed")
            if mode not in VALID_MODES:
                raise PayloadError(
                    "schema-invalid",
                    f"reply agent: replies[{i}] mode {mode!r} not in {VALID_MODES}",
                )
    return data


def build_body(body: str, addressed_id: str) -> str:
    """Agent body plus the addressed-sentinel footer, byte-for-byte."""
    return f"{body}\n\n{SENTINEL.format(id=addressed_id)}"


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
    """POST a pickup reaction on the operator's reply comment. Idempotent on
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Post threaded reply acks for reply-pr.sh (#36).")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--number", required=True)
    parser.add_argument("--raw", required=True, help="reply agent stdout capture")
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
            entry = {
                "addressed_comment_id": addressed_id,
                "bucket": bucket,
                "reaction": BUCKET_REACTION[bucket],
            }
            if bucket == "fix_claim":
                entry["in_reply_to_id"] = str(r["in_reply_to_id"])
                entry["reply_payload"] = {"body": build_body(r["body"], addressed_id)}
            plan.append(entry)
        print(json.dumps({"plan": plan}, ensure_ascii=False))
        return 0

    text_ok = 0
    react_ok = 0
    for r in replies:
        bucket = r["bucket"]
        addressed_id = str(r["addressed_comment_id"])

        # Text reply: fix_claim only. A failed POST leaves no sentinel, so the
        # next polling cycle re-detects and retries this thread (best-effort,
        # matching the prior bash loop).
        if bucket == "fix_claim":
            in_reply_to_id = str(r["in_reply_to_id"])
            full_body = build_body(r["body"], addressed_id)
            rc, err = post_reply(args.owner, args.repo, args.number, in_reply_to_id, full_body)
            if rc == 0:
                text_ok += 1
            else:
                print(
                    f"reply POST failed for comment {in_reply_to_id}: {err.strip()}",
                    file=sys.stderr,
                )

        # Pickup reaction: every bucket. Idempotent, so no dedup needed.
        content = BUCKET_REACTION[bucket]
        rc, err = post_reaction(args.owner, args.repo, addressed_id, content)
        if rc == 0:
            react_ok += 1
        else:
            print(
                f"reaction POST failed for comment {addressed_id}: {err.strip()}",
                file=sys.stderr,
            )

    print(
        f"done — {text_ok}/{fix} replies, {react_ok}/{len(replies)} reactions posted",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
