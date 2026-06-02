"""Single-process reply poster (#36).

reply-pr.sh used to build reply bodies through bash -> jq -> `while read -r` ->
gh: three layers where a missing `read -r` or a stray `jq -r` silently mangles
`\\n` / `\\t` / `\\` / backticked-regex payloads. This builds the JSON in one
process and hands it to `gh --input -`, so the body bytes stay intact end to
end; tests/test_post_reply.py pins the round-trip.

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

REQUIRED = ("in_reply_to_id", "addressed_comment_id", "body")
VALID_MODES = ("confirmed", "pushback")

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
    Mode is optional and defaults to `confirmed` (#37); normalised here so
    downstream consumers can rely on it always being set.
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Post threaded reply acks for reply-pr.sh (#36).")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--number", required=True)
    parser.add_argument("--raw", required=True, help="reply agent stdout capture")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the {body: ...} payloads that would be posted, do not call gh",
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
    confirmed = sum(1 for r in replies if r["mode"] == "confirmed")
    pushback = sum(1 for r in replies if r["mode"] == "pushback")
    print(
        f"{len(replies)} reply/replies ready ({confirmed} confirmed, {pushback} pushback)",
        file=sys.stderr,
    )
    if not replies:
        return 0

    if args.dry_run:
        posts = [
            {
                "in_reply_to_id": str(r["in_reply_to_id"]),
                "payload": {"body": build_body(r["body"], str(r["addressed_comment_id"]))},
            }
            for r in replies
        ]
        print(json.dumps({"posts": posts}, ensure_ascii=False))
        return 0

    post_ok = 0
    for r in replies:
        in_reply_to_id = str(r["in_reply_to_id"])
        full_body = build_body(r["body"], str(r["addressed_comment_id"]))
        rc, err = post_reply(args.owner, args.repo, args.number, in_reply_to_id, full_body)
        if rc == 0:
            post_ok += 1
        else:
            # Best-effort, matching the prior bash loop: a failed POST leaves no
            # sentinel on the thread, so the next polling cycle retries it.
            print(f"reply POST failed for comment {in_reply_to_id}: {err.strip()}", file=sys.stderr)

    print(f"done — posted {post_ok}/{len(replies)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
