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

# Attribute every reply-path line to this PR, so its lines stay readable amid
# the interleaved review-path lines of other PRs in the same cycle.
log_set_pr_context "$REPO" "$PR_NUMBER"

# Same per-PR colour as review-pr.sh's (identical "repo#pr" hash key), so a
# PR's colour matches across both scripts if they ever run concurrently.
PR_COLOR_START="$(_sgr "${_LOG_PR_PALETTE[$(pr_color_index "${REPO}#${PR_NUMBER}")]}")"
PR_COLOR_RESET="$(_sgr 0)"

GITHUB_USER="$(gh api user --jq '.login')"
HEAD_OID=""

log_info "checking for unaddressed replies"

# List all inline review comments on the PR. --paginate handles >30 comments.
# https://docs.github.com/en/rest/pulls/comments
COMMENTS_JSON="$(gh api --paginate "repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/comments")"

# Find unaddressed reply threads via the Reply sentinel (#39). Replaces the V2
# `user.login != $login` filter which was dormant in 1-operator setups (daemon
# and operator share an identity). Multi-user behavior unchanged. The scan
# matches both the old `addressed:` wire and the current `reply:` so threads
# acked before the #79 rename are not re-dispatched during the transition.
THREADS_JSON="$(jq --arg login "$GITHUB_USER" --arg provenance "$PROVENANCE_TAG" \
  -f "$SCRIPT_DIR/detect-replies.jq" <<<"$COMMENTS_JSON")"

THREAD_COUNT="$(jq 'length' <<<"$THREADS_JSON")"
if [[ "$THREAD_COUNT" -eq 0 ]]; then
  log_info "no unaddressed replies"
  exit 0
fi

log_info "$THREAD_COUNT unaddressed reply thread(s)"

# One GraphQL read maps each Reply thread's comment databaseIds to two node ids:
# the thread's PRRT_ id (so a settled verdict can resolve its thread, #75) and
# each comment's own id (so the resolution stamp writes via the same GraphQL
# mutation the commit path uses, #163). The Finding the operator replied to is the
# thread root, so parent_finding.comment_id appears in both maps. Best-effort: on
# failure (or a thread past the first-100 page) both stay empty and create_reply.py
# posts the ack but skips resolution and the stamp, rather than failing the reply.
gql_err="$(mktemp -t pr-review-reply-gql.XXXXXX)"
# $owner/$repo/$pr are GraphQL variables, not shell vars; keep them literal.
# shellcheck disable=SC2016
if gql_response="$(gh api graphql \
  -f query='query($owner:String!,$repo:String!,$pr:Int!){
    repository(owner:$owner,name:$repo){
      pullRequest(number:$pr){
        reviewThreads(first:100){
          nodes{ id comments(first:50){ nodes{ databaseId id } } }
        }
      }
    }
  }' -f owner="$OWNER" -f repo="$REPO" -F pr="$PR_NUMBER" 2>"$gql_err")"; then
  thread_map="$(jq '[.data.repository.pullRequest.reviewThreads.nodes[]
         | .id as $tid | .comments.nodes[]
         | {key: (.databaseId | tostring), value: $tid}] | from_entries' <<<"$gql_response")"
  comment_node_map="$(jq '[.data.repository.pullRequest.reviewThreads.nodes[]
         | .comments.nodes[]
         | {key: (.databaseId | tostring), value: .id}] | from_entries' <<<"$gql_response")"
  THREADS_JSON="$(jq --argjson map "$thread_map" --argjson cmap "$comment_node_map" \
    'map(.thread_id = ($map[.parent_finding.comment_id] // null)
       | .parent_finding.comment_node_id = ($cmap[.parent_finding.comment_id] // null))' \
    <<<"$THREADS_JSON")"
else
  log_err "reviewThreads query failed; thread resolution skipped this cycle: $(<"$gql_err")"
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

# Drop any thread whose claimed fix commit has not reached HEAD yet, so the
# agent never verifies a fix claim against pre-fix code and pushes back on a
# correct operator (the reply-race, sa#163). A deferred thread stays
# unaddressed and is redispatched next cycle once the push lands.
KEPT_THREADS='[]'
DEFERRED_COUNT=0
_thread_total="$(jq 'length' <<<"$THREADS_JSON")"
for ((_i = 0; _i < _thread_total; _i++)); do
  _thread="$(jq -c ".[$_i]" <<<"$THREADS_JSON")"
  _reply_body="$(jq -r '.operator_reply.body // ""' <<<"$_thread")"
  if reply_defers_on_unreachable_fix "$_reply_body" "$HEAD_REPO"; then
    DEFERRED_COUNT=$((DEFERRED_COUNT + 1))
    log_info "deferring a reply thread: its claimed fix commit is not in HEAD yet"
    continue
  fi
  KEPT_THREADS="$(jq -c --argjson t "$_thread" '. + [$t]' <<<"$KEPT_THREADS")"
done
THREADS_JSON="$KEPT_THREADS"
if [[ "$(jq 'length' <<<"$THREADS_JSON")" -eq 0 ]]; then
  log_info "all $DEFERRED_COUNT reply thread(s) deferred to next cycle"
  exit 0
fi

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
  # claude's own stderr (e.g. its non-interactive workspace-trust notice,
  # expected every run since a fresh scratch clone is never pre-trusted) goes
  # to a sidecar file rather than flooding the daemon log unlabeled.
  run_with_timeout "$REPLY_AGENT_TIMEOUT" \
    claude -p "/reply-pr $PR_URL --threads $THREADS_BASENAME" \
    --output-format stream-json --verbose \
    2>"$RAW_FILE.stderr" |
    python3 "$SCRIPT_DIR/stream_format.py" --raw-out "$RAW_FILE" \
      --label "${PR_COLOR_START}pr${PR_NUMBER}:reply${PR_COLOR_RESET}" \
      --cost-out "$RAW_FILE.cost"
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
if [[ -s "$RAW_FILE.cost" ]]; then
  log_info "reply cost: \$$(cat "$RAW_FILE.cost")"
fi

log_step "posting replies"
POST_ERR="$(mktemp -t pr-review-reply-post.XXXXXX)"
# Extract + validate + POST in one Python process (#36); keeps the body bytes
# intact end to end (see create_reply.py). stderr carries progress, and on
# failure a `category=` line feeds log_failure.
if python3 "$SCRIPT_DIR/create_reply.py" \
  --owner "$OWNER" --repo "$REPO" --number "$PR_NUMBER" \
  --head-sha "$HEAD_OID" --head-owner "$HEAD_REPO_OWNER" --head-repo "$HEAD_REPO_NAME" \
  --raw "$RAW_FILE" --threads "$THREADS_FILE" 2>"$POST_ERR"; then
  cat "$POST_ERR" >&2
else
  cat "$POST_ERR" >&2
  category="$(grep -m1 '^category=' "$POST_ERR" | cut -d= -f2 || true)"
  log_failure "${category:-post-failed}" "$PR_URL" "$HEAD_OID" "reply posting failed"
  exit 1
fi
