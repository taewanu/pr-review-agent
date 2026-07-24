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

for cmd in gh claude jq git python3 openssl curl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log_err "missing '$cmd' on PATH"
    exit 1
  fi
done
# App identity (ADR 0036): the App key must be readable; no `gh auth login` is
# required. The reply path renders no footer, so it needs no App-owner probe.
if [[ ! -r "$APP_KEY_PATH" ]]; then
  log_err "App private key not readable at $APP_KEY_PATH — place it there or set APP_KEY_PATH (ADR 0036)"
  exit 1
fi

KEEP_SCRATCH=0
APP_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-scratch)
      KEEP_SCRATCH=1
      shift
      ;;
    --app-id)
      if [[ $# -lt 2 ]]; then
        log_err "--app-id requires a value"
        exit 1
      fi
      APP_ID="$2"
      shift 2
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

# Reply-scoped per-PR lock (#198), same double-post rationale as the review
# path's #67 lock: a manual run overlapping a daemon tick would both list this
# PR's comments before either's ack posts, and both dispatch the same thread.
# The key is suffixed so it never contends with the review lock — the two
# paths post to disjoint surfaces and poll.sh orders them within a tick.
if ! REPLY_LOCK_FILE="$(acquire_pr_lock "$OWNER" "$REPO" "${PR_NUMBER}-reply")"; then
  log_info "reply pass already in progress for ${OWNER}/${REPO}#${PR_NUMBER}, skipping"
  exit 0
fi
# Replaced by the fuller cleanup trap once the scratch clone exists; that
# cleanup releases the lock too.
trap 'release_pr_lock "${REPLY_LOCK_FILE:-}"' EXIT

# App identity (ADR 0036): resolve this process's installation and warm its token
# in the main shell so the wrapped gh calls below reuse one token. app_auth_init
# also sets PRA_BOT_LOGIN_REST, the bot's REST login the reply gate matches on.
# --app-id comes from poll.sh; the manual one-shot resolves it from .env.
[[ -n "$APP_ID" ]] || APP_ID="$(resolve_tunable GITHUB_APP_ID "$SCRIPT_DIR/../.env")"
if [[ -z "$APP_ID" ]]; then
  log_err "no GITHUB_APP_ID (pass --app-id or set it in .env) — cannot authenticate as the App (ADR 0036)"
  exit 1
fi
if ! app_auth_init "$OWNER" "$REPO" "$APP_ID"; then
  log_err "App not installed on ${OWNER}/${REPO}, or the installation probe failed"
  exit 1
fi
app_auth_warm || {
  log_err "could not mint an App token for ${OWNER}/${REPO}"
  exit 1
}

HEAD_OID=""

log_info "checking for unaddressed replies"

# List all inline review comments on the PR. --paginate handles >30 comments;
# flatten_pages merges its per-page arrays so the jq filter below runs once
# over one array instead of once per page-document (#195).
# https://docs.github.com/en/rest/pulls/comments
COMMENTS_JSON="$(run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
  gh api --paginate "repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/comments" | flatten_pages)"

# Select unaddressed reply threads: a non-bot reply (ADR 0036 decision 9) under
# one of the bot's own Findings, not already acked. detect-replies.jq matches the
# parent Finding's author against the bot's REST login and excludes bot repliers
# by user.type, which retires the old body-text provenance self-exclusion (#153).
THREADS_JSON="$(jq --arg login "$PRA_BOT_LOGIN_REST" \
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
if gql_response="$(run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
  gh api graphql \
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
meta="$(run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
  gh pr view "$PR_URL" --json headRepository,headRepositoryOwner,headRefName,headRefOid)"
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
  release_pr_lock "${REPLY_LOCK_FILE:-}"
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
run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
  gh repo clone "$HEAD_REPO" "$SCRATCH" -- --quiet --depth=1 --no-tags
(
  cd "$SCRATCH"
  git fetch --quiet --depth=1 origin "refs/pull/${PR_NUMBER}/head"
  git checkout --quiet --detach "$HEAD_OID"
)

# Bundle operator's agent defs into the scratch (ADR 0007).
bundle_operator_agents "$SCRATCH"

# Bare filenames so the prompt's named path survives a $TMPDIR with spaces
# (same reason as review-pr.sh's diff handling).
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

# The reply agent re-checks operator fix claims against HEAD, reasoning the review
# roles' equal, so it shares their REVIEW_MODEL dial (#209) rather than carrying
# its own.
REVIEW_MODEL="$(resolve_review_model "$SCRIPT_DIR/../.env")"
# Named in the log for the reason review-pr.sh gives: an unnamed model fails silently.
log_info "model: ${REVIEW_MODEL}"

reply_rc=0
(
  # Same shared claude_slot pool as review-pr.sh's roles/editor/judge-fix
  # (ADR 0023 revision): the reply agent ran outside the pool, so with
  # MAX_PARALLEL > 1 the concurrent claude count exceeded it (#197).
  slot="$(acquire_claude_slot reply)"
  cd "$SCRATCH"
  rc=0
  # claude's own stderr (e.g. its non-interactive workspace-trust notice,
  # expected every run since a fresh scratch clone is never pre-trusted) goes
  # to a sidecar file rather than flooding the daemon log unlabeled.
  # Directly prompted like review-pr.sh's roles and editor (ADR 0038): the
  # agent body (frontmatter stripped) is the system prompt, the prompt names
  # the inputs, and slash commands are disabled because ADR 0034 measured the
  # wrapper as pure forwarding overhead.
  reply_sys="$SCRATCH/.pr-review-reply-sys.md"
  awk 'BEGIN { n = 0 } /^---$/ { n++; next } n >= 2 { print }' \
    ".claude/agents/review-agent-reply.md" >"$reply_sys"
  reply_prompt="$SCRATCH/.pr-review-reply-prompt.txt"
  {
    printf 'Process these reply threads per your instructions and emit the replies JSON.\n'
    printf 'The PR under review: %s\n' "$PR_URL"
    printf 'The unaddressed reply threads are at: %s\n' "$THREADS_BASENAME"
  } >"$reply_prompt"
  run_with_timeout "$REPLY_AGENT_TIMEOUT" \
    claude -p --append-system-prompt-file "$reply_sys" \
    --model "$REVIEW_MODEL" \
    --tools Read Grep Glob Bash --strict-mcp-config --setting-sources project \
    --disable-slash-commands \
    --output-format stream-json --verbose <"$reply_prompt" \
    2>"$RAW_FILE.stderr" |
    python3 "$SCRIPT_DIR/stream_format.py" --raw-out "$RAW_FILE" \
      --label "${PR_COLOR_START}pr${PR_NUMBER}:reply${PR_COLOR_RESET}" \
      --cost-out "$RAW_FILE.cost" ||
    rc=$?
  release_claude_slot "$slot"
  exit "$rc"
) || reply_rc=$?
if [[ "$reply_rc" -eq "$TIMEOUT_EXIT" ]]; then
  log_failure "$FAIL_REPLY_TIMEOUT" "$PR_URL" "$HEAD_OID" \
    "reply agent exceeded ${REPLY_AGENT_TIMEOUT}s"
  exit 1
fi
if [[ ! -s "$RAW_FILE" ]]; then
  log_failure "$FAIL_EMPTY_STDOUT" "$PR_URL" "$HEAD_OID" "reply agent produced no output"
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
if run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
  python3 "$SCRIPT_DIR/create_reply.py" \
  --owner "$OWNER" --repo "$REPO" --number "$PR_NUMBER" \
  --head-sha "$HEAD_OID" --head-owner "$HEAD_REPO_OWNER" --head-repo "$HEAD_REPO_NAME" \
  --raw "$RAW_FILE" --threads "$THREADS_FILE" 2>"$POST_ERR"; then
  cat "$POST_ERR" >&2
else
  cat "$POST_ERR" >&2
  category="$(grep -m1 '^category=' "$POST_ERR" | cut -d= -f2 || true)"
  log_failure "${category:-$FAIL_POST_FAILED}" "$PR_URL" "$HEAD_OID" "reply posting failed"
  exit 1
fi
