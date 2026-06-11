#!/usr/bin/env bash
# reply-pr.sh — ack operator inline replies on prior findings. Lists
# unaddressed threads, dispatches the reply agent to classify and verify each
# claim against the file at HEAD, then posts the acks.
#
# Fix claims and questions get a threaded text reply carrying the Reply
# sentinel: a fix claim resolves to `confirmed`/`pushback` (file vs the
# operator's claim), a question to `stands`/`withdrawn` (the finding holds, or
# is conceded as a false positive). Every thread also gets an Ack reaction on
# the operator's reply comment, chosen by bucket: eyes ("seen") for fix claims
# and questions, +1 ("noted") for acknowledgments. An acknowledgment gets the
# reaction as its terminal ack, plus the Reply sentinel embedded in the parent
# finding so the next cycle skips it (#79).
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
  # (a body-bearing reply for fix claims and questions, or an acknowledgment
  # parent finding body).
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
          # exclude our own body-bearing acks (the reply body carries the sentinel)
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

# One GraphQL read serves three needs the REST comments payload can't:
#   - the PRRT_ review-thread node id per thread, mapped back from each thread's
#     comment databaseIds (the Finding the operator replied to is the thread
#     root, so parent_finding.comment_id appears in that map) — drives both the
#     review wrapper (#38) and resolution (#75);
#   - the PR node id, the addPullRequestReview target for the wrapper (#38);
#   - a stale reply-wrapper review (a prior tick that added replies but failed to
#     submit), discarded before this tick opens its own (#38). Matched by the
#     hidden reply-review marker in the review body, NOT just "viewer + PENDING":
#     on others' PRs the Finding review is left PENDING as the ADR 0008 safety
#     gate, so a state-only filter would delete the operator's in-flight draft
#     (what post-review.sh refuses to do). Identity-scoped too, so a human
#     reviewer's draft on a multi-user PR is never touched.
# Best-effort read: on failure (or a thread past the first-100 page) PR_NODE_ID /
# thread_id stay empty and post_reply.py degrades to a detached REST reply (the
# pre-#38 path) and skips resolution, rather than failing the reply.
PR_NODE_ID=""
EXISTING_PENDING_REVIEW_ID=""
gql_err="$(mktemp -t pr-review-reply-gql.XXXXXX)"
# $owner/$repo/$pr are GraphQL variables, not shell vars — keep them literal.
# shellcheck disable=SC2016
if gql_response="$(gh api graphql \
  -f query='query($owner:String!,$repo:String!,$pr:Int!){
    viewer{ login }
    repository(owner:$owner,name:$repo){
      pullRequest(number:$pr){
        id
        reviewThreads(first:100){
          nodes{ id comments(first:50){ nodes{ databaseId } } }
        }
        reviews(first:50,states:[PENDING]){ nodes{ id author{ login } body } }
      }
    }
  }' -f owner="$OWNER" -f repo="$REPO" -F pr="$PR_NUMBER" 2>"$gql_err")"; then
  PR_NODE_ID="$(jq -r '.data.repository.pullRequest.id // empty' <<<"$gql_response")"
  # Only a review the daemon tagged as a reply wrapper is eligible for deletion;
  # the marker string is post_reply.py's REPLY_REVIEW_MARKER.
  EXISTING_PENDING_REVIEW_ID="$(jq -r '
    .data.viewer.login as $me
    | (.data.repository.pullRequest.reviews.nodes // [])
    | map(select(.author.login == $me and ((.body // "") | contains("pr-review-agent:reply-review"))))
    | .[0].id // empty' <<<"$gql_response")"
  thread_map="$(jq '[.data.repository.pullRequest.reviewThreads.nodes[]
         | .id as $tid | .comments.nodes[]
         | {key: (.databaseId | tostring), value: $tid}] | from_entries' <<<"$gql_response")"
  THREADS_JSON="$(jq --argjson map "$thread_map" \
    'map(.thread_id = ($map[.parent_finding.comment_id] // null))' <<<"$THREADS_JSON")"
else
  log_err "reviewThreads query failed; review wrapping + resolution skipped this cycle: $(<"$gql_err")"
fi
rm -f "$gql_err"

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

# Bound the https clone/fetch so a stalled connection aborts cleanly instead of
# hanging the loop (#121). Backstopped by poll.sh's per-PR watchdog.
arm_git_stall_timeout
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
  --head-sha "$HEAD_OID" --head-owner "$HEAD_REPO_OWNER" --head-repo "$HEAD_REPO_NAME" \
  --pr-node-id "$PR_NODE_ID" --existing-pending-review-id "$EXISTING_PENDING_REVIEW_ID" \
  --raw "$RAW_FILE" --threads "$THREADS_FILE" 2>"$POST_ERR"; then
  cat "$POST_ERR" >&2
else
  cat "$POST_ERR" >&2
  category="$(grep -m1 '^category=' "$POST_ERR" | cut -d= -f2 || true)"
  log_failure "${category:-post-failed}" "$PR_URL" "$HEAD_OID" "reply posting failed"
  exit 1
fi
