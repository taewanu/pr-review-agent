#!/usr/bin/env bash
# reply-pr.sh — ack operator inline replies on prior findings. Lists
# unaddressed threads, dispatches the reply agent to verify each claim
# against the file at HEAD, posts threaded acks with addressed-sentinel.
#
# Replies are emitted as `confirmed` (file matches operator's claim) or
# `pushback` (file shows the mismatch). Non-claim replies (thanks, questions)
# get no reply and remain unaddressed until the operator posts a fix claim.
#
# Usage:
#   bash daemon/reply-pr.sh [--keep-scratch] <pr-url>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=daemon/lib.sh disable=SC1091
source "$SCRIPT_DIR/lib.sh"

for cmd in gh claude jq git python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log_err "missing '$cmd' on PATH"
    exit 1
  fi
done
if ! gh auth status >/dev/null 2>&1; then
  log_err "gh not authenticated — run 'gh auth login' first"
  exit 1
fi
derive_project_identity "$SCRIPT_DIR/.."

KEEP_SCRATCH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-scratch)
      KEEP_SCRATCH=1
      shift
      ;;
    -*)
      log_err "unknown flag: $1"
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

if [[ $# -ne 1 ]]; then
  log_err "usage: reply-pr.sh [--keep-scratch] <pr-url>"
  exit 1
fi

PR_URL="$1"

if [[ ! "$PR_URL" =~ ^https://github\.com/([^/]+)/([^/]+)/pull/([0-9]+) ]]; then
  log_err "invalid PR URL: $PR_URL"
  exit 1
fi
OWNER="${BASH_REMATCH[1]}"
REPO="${BASH_REMATCH[2]}"
PR_NUMBER="${BASH_REMATCH[3]}"

GITHUB_USER="$(gh api user --jq '.login')"
HEAD_OID=""

log_info "checking for unaddressed replies: $PR_URL"

# List all inline review comments on the PR. --paginate handles >30 comments.
# https://docs.github.com/en/rest/pulls/comments
COMMENTS_JSON="$(gh api --paginate "repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/comments")"

# Find unaddressed reply threads via the addressed-sentinel (#39). Replaces
# the V2 `user.login != $login` filter which was dormant in 1-operator setups
# (daemon and operator share an identity). Multi-user behavior unchanged.
THREADS_JSON="$(jq --arg login "$GITHUB_USER" '
  . as $all
  # IDs we have already acked, extracted from prior acks sentinel markers.
  | [.[] | (.body // "") | scan("pr-review-agent:addressed:([0-9]+)") | .[0]]
    as $addressed
  | (map({key: (.id | tostring), value: .}) | from_entries) as $by_id
  | map(
      # Bind .id to $cur first; piping into $addressed shifts `.` to the array,
      # so a naked `.id` inside `index(...)` would fail.
      . as $cur
      | select(
          $cur.in_reply_to_id != null
          # parent must be our finding
          and ($by_id[($cur.in_reply_to_id | tostring)].user.login == $login)
          # exclude our own acks (body carries the sentinel)
          and (($cur.body // "") | test("pr-review-agent:addressed:") | not)
          # exclude replies already in the addressed-set
          and (($addressed | index($cur.id | tostring)) | not)
        )
      | . as $op
      | ($by_id[($op.in_reply_to_id | tostring)]) as $parent
      | ($parent.line // $parent.original_line) as $endL
      | ($parent.start_line // $parent.original_start_line // $endL) as $startL
      | {
          parent_finding: {
            comment_id: ($parent.id | tostring),
            path: $parent.path,
            line: $startL,
            end_line: $endL,
            body: $parent.body
          },
          operator_reply: {
            comment_id: ($op.id | tostring),
            body: $op.body
          }
        }
    )
' <<<"$COMMENTS_JSON")"

THREAD_COUNT="$(jq 'length' <<<"$THREADS_JSON")"
if [[ "$THREAD_COUNT" -eq 0 ]]; then
  log_info "no unaddressed replies"
  exit 0
fi

log_info "$THREAD_COUNT unaddressed reply thread(s)"

# Scratch clone at HEAD for the agent to verify claims via file reads. Same
# refs/pull/N/head pattern as review-pr.sh — survives deleted head branch.
meta="$(gh pr view "$PR_URL" --json headRepository,headRepositoryOwner,headRefName,headRefOid)"
HEAD_REPO_OWNER="$(jq -r '.headRepositoryOwner.login // empty' <<<"$meta")"
HEAD_REPO_NAME="$(jq -r '.headRepository.name // empty' <<<"$meta")"
HEAD_REF="$(jq -r '.headRefName // empty' <<<"$meta")"
HEAD_OID="$(jq -r '.headRefOid // empty' <<<"$meta")"
if [[ -z "$HEAD_REPO_OWNER" || -z "$HEAD_REPO_NAME" || -z "$HEAD_REF" || -z "$HEAD_OID" ]]; then
  log_err "gh pr view returned incomplete metadata for $PR_URL"
  exit 1
fi
HEAD_REPO="${HEAD_REPO_OWNER}/${HEAD_REPO_NAME}"
log_info "head: ${HEAD_REPO}@${HEAD_REF} (${HEAD_OID:0:12})"

SCRATCH="$(mktemp -d -t pr-review-reply.XXXXXX)"
POST_ERR=""
cleanup() {
  if [[ $KEEP_SCRATCH -ne 1 ]]; then
    rm -rf "$SCRATCH"
  fi
  if [[ -n "$POST_ERR" ]]; then
    rm -f "$POST_ERR"
  fi
}
trap cleanup EXIT
if [[ $KEEP_SCRATCH -eq 1 ]]; then
  log_info "scratch (will be preserved): $SCRATCH"
fi

gh repo clone "$HEAD_REPO" "$SCRATCH" -- --quiet --depth=1 --no-tags
(
  cd "$SCRATCH"
  git fetch --quiet --depth=1 origin "refs/pull/${PR_NUMBER}/head"
  git checkout --quiet --detach "$HEAD_OID"
)

# Bundle operator's agent + slash-command defs into the scratch (ADR 0007).
bundle_operator_agents "$SCRATCH"

# Bare filenames so the slash-command args survive a $TMPDIR with spaces
# (same reason as review-pr.sh's --diff handling).
THREADS_BASENAME=".pr-review-threads.json"
THREADS_FILE="$SCRATCH/$THREADS_BASENAME"
RAW_FILE="$SCRATCH/.pr-review-reply-raw.txt"
PAYLOAD_FILE="$SCRATCH/.pr-review-reply-payload.json"

printf '%s\n' "$THREADS_JSON" >"$THREADS_FILE"

log_step "running reply agent via claude -p"
(
  cd "$SCRATCH"
  claude -p "/reply-pr $PR_URL --threads $THREADS_BASENAME" >"$RAW_FILE"
)
if [[ ! -s "$RAW_FILE" ]]; then
  log_failure "empty-stdout" "$PR_URL" "$HEAD_OID" "reply agent produced no output"
  exit 1
fi

log_step "extracting payload"
# Inline extractor (schema diverges from extract-json.py). Same fence
# convention: last ```json block wins.
if ! python3 - "$RAW_FILE" >"$PAYLOAD_FILE" <<'PYEOF'
import json
import re
import sys
from pathlib import Path

raw = Path(sys.argv[1]).read_text()
matches = re.findall(r"```json\s*\n(.*?)\n```", raw, re.DOTALL)
if not matches:
    print("category=no-fence", file=sys.stderr)
    print("reply agent: no ```json fence in output", file=sys.stderr)
    sys.exit(1)
try:
    data = json.loads(matches[-1])
except json.JSONDecodeError as exc:
    print("category=parse-error", file=sys.stderr)
    print(f"reply agent: JSON decode failed: {exc}", file=sys.stderr)
    sys.exit(1)
if "replies" not in data or not isinstance(data["replies"], list):
    print("category=schema-invalid", file=sys.stderr)
    print("reply agent: missing or non-list 'replies' key", file=sys.stderr)
    sys.exit(1)
required = ("in_reply_to_id", "addressed_comment_id", "body")
valid_modes = ("confirmed", "pushback")
for i, r in enumerate(data["replies"]):
    missing = [k for k in required if k not in r]
    if missing:
        print("category=schema-invalid", file=sys.stderr)
        print(f"reply agent: replies[{i}] missing keys: {missing}", file=sys.stderr)
        sys.exit(1)
    # mode optional with `confirmed` default (#37). Normalise so downstream
    # consumers (logging, future metrics) can rely on it always being set.
    mode = r.setdefault("mode", "confirmed")
    if mode not in valid_modes:
        print("category=schema-invalid", file=sys.stderr)
        print(f"reply agent: replies[{i}] mode {mode!r} not in {valid_modes}", file=sys.stderr)
        sys.exit(1)
print(json.dumps(data))
PYEOF
then
  log_failure "extract-failed" "$PR_URL" "$HEAD_OID" "reply agent payload invalid"
  exit 1
fi

REPLY_COUNT="$(jq '.replies | length' "$PAYLOAD_FILE")"
CONFIRMED_COUNT="$(jq '[.replies[] | select(.mode == "confirmed")] | length' "$PAYLOAD_FILE")"
PUSHBACK_COUNT="$(jq '[.replies[] | select(.mode == "pushback")] | length' "$PAYLOAD_FILE")"
log_info "$REPLY_COUNT reply/replies ready (${CONFIRMED_COUNT} confirmed, ${PUSHBACK_COUNT} pushback)"
if [[ "$REPLY_COUNT" -eq 0 ]]; then
  exit 0
fi

# Post each reply. /comments/{id}/replies inherits path+line from the parent;
# body is the only field. Sentinel footer flags addressed-by-us so the next
# polling cycle's sentinel-based detection (#39) skips this reply.
log_step "posting replies"
POST_ERR="$(mktemp -t pr-review-reply-post.XXXXXX)"
post_ok=0
while IFS= read -r reply; do
  in_reply_to_id="$(jq -r '.in_reply_to_id' <<<"$reply")"
  addressed_id="$(jq -r '.addressed_comment_id' <<<"$reply")"
  body="$(jq -r '.body' <<<"$reply")"

  full_body="${body}

<!-- pr-review-agent:addressed:${addressed_id} -->"

  # jq builds {body: "..."} so `gh api --input -` sees a proper JSON payload.
  # Earlier draft used `-f body=@-`, but `-f`/--raw-field doesn't expand `@-`
  # (that's `-F`/--field), so the literal string "@-" was getting posted.
  body_json="$(jq -n --arg b "$full_body" '{body: $b}')"

  if printf '%s' "$body_json" | gh api \
    --method POST \
    "repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/comments/${in_reply_to_id}/replies" \
    --input - >"$POST_ERR" 2>&1; then
    post_ok=$((post_ok + 1))
  else
    log_err "reply POST failed for comment ${in_reply_to_id}: $(<"$POST_ERR")"
  fi
done < <(jq -c '.replies[]' "$PAYLOAD_FILE")

log_step "done — posted $post_ok/$REPLY_COUNT"
