#!/usr/bin/env bash
# reply-pr.sh — ack operator inline replies on prior findings. Lists
# unaddressed threads, dispatches the reply agent to classify and verify each
# claim against the file at HEAD, then posts the acks.
#
# Fix claims get a threaded text reply with the Reply sentinel: `confirmed`
# (file matches the operator's claim) or `pushback` (file shows the mismatch).
# Every thread also gets an Ack reaction on the operator's reply comment,
# chosen by bucket: eyes ("seen") for fix claims and questions, +1 ("noted")
# for acknowledgments. Non-claim replies get the reaction as their terminal
# ack instead of the prior silence, plus the Reply sentinel embedded in the
# parent finding so the next cycle skips them (#79).
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

# Find unaddressed reply threads via the Reply sentinel (#39). Replaces the V2
# `user.login != $login` filter which was dormant in 1-operator setups (daemon
# and operator share an identity). Multi-user behavior unchanged. The scan
# matches both the old `addressed:` wire and the current `reply:` so threads
# acked before the #79 rename are not re-dispatched during the transition.
THREADS_JSON="$(jq --arg login "$GITHUB_USER" '
  . as $all
  # Operator-reply IDs we have already acked, read from prior Reply sentinels
  # (a fix_claim reply body, or a non-claim parent finding body).
  | [.[] | (.body // "") | scan("pr-review-agent:(?:addressed|reply):([0-9]+)") | .[0]]
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
          # exclude our own fix_claim acks (reply body carries the sentinel)
          and (($cur.body // "") | test("pr-review-agent:(addressed|reply):") | not)
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

printf '%s\n' "$THREADS_JSON" >"$THREADS_FILE"

log_step "running reply agent via claude -p"
# Wall-clock backstop (#76). #51 removed the cause of the 745s reply-runtime
# spike, but only as a prompt instruction; this enforces a ceiling no matter how
# the agent behaves. Sized above the observed legitimate ceiling (review runs
# land ~60-120s; a reply run does less) and below the runaway. Partial output on
# timeout is discarded, not parsed.
REPLY_AGENT_TIMEOUT="${REPLY_AGENT_TIMEOUT:-300}"
reply_rc=0
(
  cd "$SCRATCH"
  run_with_timeout "$REPLY_AGENT_TIMEOUT" \
    claude -p "/reply-pr $PR_URL --threads $THREADS_BASENAME" >"$RAW_FILE"
) || reply_rc=$?
if [[ "$reply_rc" -eq "$TIMEOUT_EXIT" ]]; then
  log_failure "reply-timeout" "$PR_URL" "$HEAD_OID" \
    "reply agent exceeded ${REPLY_AGENT_TIMEOUT}s"
  exit 1
fi
if [[ ! -s "$RAW_FILE" ]]; then
  log_failure "empty-stdout" "$PR_URL" "$HEAD_OID" "reply agent produced no output"
  exit 1
fi

log_step "posting replies"
POST_ERR="$(mktemp -t pr-review-reply-post.XXXXXX)"
# Extract + validate + POST in one Python process (#36); keeps the body bytes
# intact end to end (see post_reply.py). stderr carries progress, and on
# failure a `category=` line feeds log_failure.
if python3 "$SCRIPT_DIR/post_reply.py" \
  --owner "$OWNER" --repo "$REPO" --number "$PR_NUMBER" \
  --head-sha "$HEAD_OID" \
  --raw "$RAW_FILE" --threads "$THREADS_FILE" 2>"$POST_ERR"; then
  cat "$POST_ERR" >&2
else
  cat "$POST_ERR" >&2
  category="$(grep -m1 '^category=' "$POST_ERR" | cut -d= -f2 || true)"
  log_failure "${category:-post-failed}" "$PR_URL" "$HEAD_OID" "reply posting failed"
  exit 1
fi
