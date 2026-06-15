# shellcheck shell=bash
# lib.sh — shared helpers sourced by other daemon scripts. Not executable on its own.

log_info() {
  printf '[pr-review-agent] %s\n' "$*" >&2
}

log_err() {
  printf '[pr-review-agent] ERROR: %s\n' "$*" >&2
}

# log_step <message>
# $SECONDS is per-process — call only from the orchestrator (review-pr.sh), not
# from sub-scripts whose clocks start at 0.
log_step() {
  printf '[pr-review-agent] %s (+%ds)\n' "$*" "${SECONDS}" >&2
}

# log_failure <category> <pr-url> <head-sha> <reason>
# Positional fields per ADR 0005 so log scrapers don't re-parse prose.
log_failure() {
  local category="$1" url="$2" sha="$3" reason="$4"
  printf '[pr-review-agent] failure: %s pr=%s sha=%s reason=%s\n' \
    "$category" "$url" "$sha" "$reason" >&2
}

# Exit status the shell reports when a process is killed by SIGALRM (128 + 14).
# run_with_timeout returns this on timeout; callers branch on it to route to the
# ADR 0005 failure path instead of parsing the truncated output.
# shellcheck disable=SC2034  # consumed by reply-pr.sh / review-pr.sh
readonly TIMEOUT_EXIT=142

# run_with_timeout <seconds> <command...>
# Caps a command's wall-clock runtime. macOS has no coreutils `timeout`, so we
# lean on perl's alarm: it arms a SIGALRM timer, then `exec`s the command into
# the same process. The timer survives exec (it is kernel state, not perl's
# memory) while exec resets SIGALRM to its default of terminate, so an
# over-running command dies at the cap. Returns the command's own exit status,
# or $TIMEOUT_EXIT on timeout. Orphaned grandchildren may briefly outlive the
# kill, acceptable for a backstop: stdout closes and the tick fails over anyway.
run_with_timeout() {
  local secs="$1"
  shift
  perl -e 'alarm shift; exec @ARGV or exit 127' "$secs" "$@"
}

# Outer per-PR watchdog cap (#121). run_with_timeout bounds only the inner
# `claude -p` call (300s default); the network steps around it — `gh repo clone`,
# the per-PR `git fetch`, `gh pr diff`, the `gh api` posts — were unbounded, so a
# stalled fetch after a laptop sleep once froze the whole serial loop for ~10h.
# This caps a per-PR step end-to-end. It must stay above the inner agent cap plus
# clone/fetch/post overhead, or a legitimately slow review is killed: 600 ≈ 2×
# the inner cap. Override for tests.
readonly PER_PR_TIMEOUT="${PER_PR_TIMEOUT:-600}"

# run_with_pr_timeout <failure-category> <pr-url> <head-sha> <command...>
# Wraps one per-PR step (review-pr.sh / reply-pr.sh dispatch) in PER_PR_TIMEOUT
# so a hang in any network step fails over to the next tick instead of wedging
# the loop (#121). On timeout, emits the ADR 0005 structured failure line and
# returns $TIMEOUT_EXIT; otherwise passes the command's own exit status through,
# so a genuine non-zero is never misread as a timeout. The broad backstop:
# whatever stalls — git, gh, claude, DNS — is bounded here.
run_with_pr_timeout() {
  local category="$1" url="$2" sha="$3"
  shift 3
  local rc=0
  run_with_timeout "$PER_PR_TIMEOUT" "$@" || rc=$?
  if [[ "$rc" -eq "$TIMEOUT_EXIT" ]]; then
    log_failure "${category}-timeout" "$url" "$sha" "per-PR step exceeded ${PER_PR_TIMEOUT}s"
  fi
  return "$rc"
}

# arm_git_stall_timeout
# Exports git's low-speed abort thresholds so an https clone/fetch over a stalled
# connection (laptop sleep, network drop) aborts with an error in ~30s instead of
# hanging forever (#121, the exact failure seen). Layered under the poll.sh
# per-PR watchdog: this kills the common https stall cleanly so the script's own
# cleanup runs (scratch removed, lock released), while the watchdog backstops
# everything the env can't reach (ssh transport, gh api, DNS). Exported, not just
# set, so the git child processes inherit it. Override either threshold for tests.
arm_git_stall_timeout() {
  export GIT_HTTP_LOW_SPEED_LIMIT="${GIT_HTTP_LOW_SPEED_LIMIT:-1000}"
  export GIT_HTTP_LOW_SPEED_TIME="${GIT_HTTP_LOW_SPEED_TIME:-30}"
}

# True when $2 (the prior-reviewed SHA) is an ancestor of HEAD in the repo at
# $1, i.e. the branch advanced by a fast-forward so an incremental `$2..HEAD`
# diff still reflects the whole PR. False after a force-push or rebase: the tips
# have diverged, so `$2..HEAD` surfaces unrelated base commits and cancels the
# PR's own change (#123). Also false when ancestry can't be resolved locally (a
# shallow clone lacking $2's history), so callers fall back to the full PR diff.
is_fast_forward() {
  git -C "$1" merge-base --is-ancestor "$2" HEAD 2>/dev/null
}

# State tracking for same-SHA dedup. One file per PR. Layered behind the
# sentinel-based dedup (ADR 0006) as a fallback when GitHub's reviews API is
# unavailable.
#
# Override $PR_REVIEW_STATE_DIR for tests.
_state_dir() {
  printf '%s' "${PR_REVIEW_STATE_DIR:-$HOME/.pr-review-agent}"
}

_state_path() {
  printf '%s/%s-%s-%s.json' "$(_state_dir)" "$1" "$2" "$3"
}

# state_read <owner> <repo> <pr-number>
# Emits the state JSON on stdout. Missing file → '{}' and exit 0 (absence is
# the normal case for first-ever-tick on a PR, not an error).
state_read() {
  local path
  path="$(_state_path "$1" "$2" "$3")"
  if [[ -r "$path" ]]; then
    cat "$path"
  else
    printf '{}\n'
  fi
}

# state_write <owner> <repo> <pr-number> <head-sha> <review-id>
# Atomic write. Creates the state dir if missing. The mktemp + mv pattern keeps
# concurrent ticks from observing a half-written file.
state_write() {
  local owner="$1" repo="$2" pr="$3" sha="$4" review_id="$5"
  local dir path tmp ts
  dir="$(_state_dir)"
  path="$(_state_path "$owner" "$repo" "$pr")"
  ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  mkdir -p "$dir"
  tmp="$(mktemp "$dir/.pr-review-state.XXXXXX")"
  if ! jq -n \
    --arg sha "$sha" \
    --argjson review_id "$review_id" \
    --arg ts "$ts" \
    '{last_reviewed_sha: $sha, review_id: $review_id, ts_iso: $ts}' \
    >"$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  mv "$tmp" "$path"
}

# discover_sentinel_sha <owner> <repo> <pr-number> <login>
# Reads the prior reviewed SHA from the most recent operator-authored review or
# PR comment carrying the ADR 0006 sentinel. Comments are scanned too so the SHA
# survives a submit-modal body-wipe that strips it from the review (#49). Exit
# codes drive the caller's fallback policy:
#   0 — sentinel found (stdout has SHA)
#   1 — APIs ok, nothing carries a sentinel (stdout empty)
#   2 — the reviews API call failed (network, 5xx, auth break)
# 1 lets the caller fall through to state and accept first-review on empty;
# 2 demands the caller skip the tick instead of misreading "could not check"
# as "no prior review". `--paginate` keeps PRs with >30 reviews/comments from
# silent truncation.
discover_sentinel_sha() {
  local owner="$1" repo="$2" pr="$3" login="$4"
  local reviews_json comments_json stderr_capture
  stderr_capture="$(mktemp -t pr-review-discover.XXXXXX)"
  if ! reviews_json="$(gh api --paginate "repos/${owner}/${repo}/pulls/${pr}/reviews" 2>"$stderr_capture")"; then
    log_err "sentinel discovery: gh api .../pulls/${pr}/reviews failed: $(<"$stderr_capture")"
    rm -f "$stderr_capture"
    return 2
  fi
  # Comments are best-effort: a failure here degrades to reviews-only rather
  # than escalating to a return-2 skip, since reviews (the primary source)
  # already succeeded.
  if ! comments_json="$(gh api --paginate "repos/${owner}/${repo}/issues/${pr}/comments" 2>"$stderr_capture")"; then
    log_err "sentinel discovery: gh api .../issues/${pr}/comments failed (degrading to reviews-only): $(<"$stderr_capture")"
    comments_json="[]"
  fi
  rm -f "$stderr_capture"
  # One timeline across both sources, newest first; a still-pending review's
  # submitted_at is null, so fall back to created_at.
  local sha
  sha="$(jq -rn --argjson reviews "$reviews_json" --argjson comments "$comments_json" --arg login "$login" '
    ([$reviews[]  | {login: .user.login, ts: (.submitted_at // .created_at), body}]
     + [$comments[] | {login: .user.login, ts: .created_at, body}])
    | [.[] | select(.login == $login)]
    | sort_by(.ts) | reverse
    | [.[] | .body | capture("<!-- pr-review-agent:sha:(?<sha>[0-9a-f]{40}) -->"; "")? | .sha]
    | first // ""
  ')"
  if [[ -n "$sha" ]]; then
    printf '%s\n' "$sha"
    return 0
  fi
  return 1
}

# Per-PR review lock. ADR 0008's own-PR auto-submit dropped the implicit "one
# pending review per PR" constraint that serialized concurrent reviews (a second
# pending POST was rejected with 422). A submitted COMMENT review has no such
# limit, so a manual review-pr.sh run overlapping a daemon tick would double-post
# (#67); these helpers reinstate serialization locally. macOS has no `flock`, so
# the lock is a noclobber lockfile: `set -o noclobber` makes `>` fail when the
# file exists, and the holder PID and epoch are written in the same redirect, so
# a half-written lock is never observable.
#
# Override $PR_REVIEW_LOCK_STALE_SECONDS for tests.
_lock_path() {
  printf '%s/%s-%s-%s.lock' "$(_state_dir)" "$1" "$2" "$3"
}

# acquire_pr_lock <owner> <repo> <pr-number>
# Non-blocking. On success prints the lock path on stdout and returns 0. Returns
# 1 if a live lock is already held. A lock whose holder process is gone, or that
# has outlived PR_REVIEW_LOCK_STALE_SECONDS (default 1800, longer than any real
# review), is treated as abandoned and reclaimed.
acquire_pr_lock() {
  local owner="$1" repo="$2" pr="$3"
  local dir lockfile stale holder created now
  dir="$(_state_dir)"
  mkdir -p "$dir"
  lockfile="$(_lock_path "$owner" "$repo" "$pr")"
  stale="${PR_REVIEW_LOCK_STALE_SECONDS:-1800}"
  if (
    set -o noclobber
    printf '%s %s\n' "$$" "$(date +%s)" >"$lockfile"
  ) 2>/dev/null; then
    printf '%s\n' "$lockfile"
    return 0
  fi
  # Held. Reclaim only if the holder is gone or the lock outlived the stale
  # window; an empty read (a lock mid-acquisition) counts as live. The
  # reclaim-then-recreate races if two runs hit the same stale lock at once,
  # acceptable since that only follows a prior holder's crash.
  read -r holder created <"$lockfile" 2>/dev/null || true
  now="$(date +%s)"
  if { [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; } ||
    { [[ -n "$created" ]] && ((now - created > stale)); }; then
    rm -f "$lockfile"
    if (
      set -o noclobber
      printf '%s %s\n' "$$" "$(date +%s)" >"$lockfile"
    ) 2>/dev/null; then
      printf '%s\n' "$lockfile"
      return 0
    fi
  fi
  return 1
}

# release_pr_lock <lock-path>
# Removes the lock. No-op on an empty path so a cleanup trap can call it
# unconditionally even if the run exited before acquiring.
release_pr_lock() {
  local lockfile="${1:-}"
  [[ -n "$lockfile" ]] && rm -f "$lockfile"
  return 0
}

# Daemon singleton + heartbeat for the run.sh polling loop (ADR 0009). The loop
# is the scheduling driver in place of a launchd StartInterval timer; the
# singleton stops two loops (a foreground run.sh and the KeepAlive one) from both
# driving ticks, and the heartbeat makes liveness observable. Same noclobber +
# dead-holder-reclaim mechanism as the per-PR lock above, minus the stale window:
# a loop is long-lived by design, so a live holder is never aged out — only a
# dead one (a SIGKILLed loop that skipped its cleanup trap) is reclaimed.
#
# Override $PR_REVIEW_STATE_DIR for tests.
_daemon_pid_path() {
  printf '%s/daemon.pid' "$(_state_dir)"
}

_heartbeat_path() {
  printf '%s/daemon.heartbeat' "$(_state_dir)"
}

# acquire_daemon_singleton
# Non-blocking. Prints the pidfile path and returns 0 if no live loop holds it
# (reclaiming a dead holder's file); returns 1 if a live run.sh already holds it.
acquire_daemon_singleton() {
  local dir pidfile holder
  dir="$(_state_dir)"
  mkdir -p "$dir"
  pidfile="$(_daemon_pid_path)"
  if (
    set -o noclobber
    printf '%s\n' "$$" >"$pidfile"
  ) 2>/dev/null; then
    printf '%s\n' "$pidfile"
    return 0
  fi
  # Held. Reclaim only if the holder is gone; an empty read (a pidfile
  # mid-write) counts as live and is left alone.
  read -r holder <"$pidfile" 2>/dev/null || true
  if [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; then
    rm -f "$pidfile"
    if (
      set -o noclobber
      printf '%s\n' "$$" >"$pidfile"
    ) 2>/dev/null; then
      printf '%s\n' "$pidfile"
      return 0
    fi
  fi
  return 1
}

# release_daemon_singleton <pidfile>
# Removes the pidfile. No-op on an empty path so a cleanup trap can call it
# unconditionally even if the loop exited before acquiring.
release_daemon_singleton() {
  local pidfile="${1:-}"
  [[ -n "$pidfile" ]] && rm -f "$pidfile"
  return 0
}

# write_heartbeat
# Atomically stamps the heartbeat file with the current epoch, once per loop
# cycle. Liveness is then "now - heartbeat < a couple of intervals"; a stale
# stamp means the loop is dead or a tick is wedged.
write_heartbeat() {
  local dir hb tmp
  dir="$(_state_dir)"
  hb="$(_heartbeat_path)"
  mkdir -p "$dir"
  tmp="$(mktemp "$dir/.heartbeat.XXXXXX")" || return 1
  if printf '%s\n' "$(date +%s)" >"$tmp"; then
    mv "$tmp" "$hb"
  else
    rm -f "$tmp"
    return 1
  fi
}

# bundle_operator_agents <scratch-dir>
# Copies operator's agent + slash-command files from this repo's .claude/ into
# the scratch clone's .claude/ so claude -p (which loads from cwd) finds them
# without requiring target-repo setup (ADR 0007). Target-repo files (already
# present in the scratch from the clone) win via the `[[ -e dst ]] || cp`
# guard — a repo can ship its own override.
bundle_operator_agents() {
  local scratch="$1"
  local repo_root
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  mkdir -p "$scratch/.claude/agents" "$scratch/.claude/commands"
  local f base
  for f in "$repo_root/.claude/agents/review-agent-"*.md; do
    [[ -e "$f" ]] || continue
    base="$(basename "$f")"
    [[ -e "$scratch/.claude/agents/$base" ]] || cp "$f" "$scratch/.claude/agents/$base"
  done
  for f in "$repo_root/.claude/commands/review-pr.md" \
    "$repo_root/.claude/commands/edit-review.md" \
    "$repo_root/.claude/commands/reply-pr.md" \
    "$repo_root/.claude/commands/judge-fix.md"; do
    [[ -e "$f" ]] || continue
    base="$(basename "$f")"
    [[ -e "$scratch/.claude/commands/$base" ]] || cp "$f" "$scratch/.claude/commands/$base"
  done
}

# fetch_open_review_threads <owner> <repo> <pr-number>
# Emits a JSON array of the PR's review threads shaped for resolve_threads.py:
#   [{thread_id, is_resolved, root_author, root_body, path, original_line,
#     original_start_line, has_fix_note}]
# The root (oldest) comment is the Finding; resolve_threads.py filters to the
# open, daemon-owned ones. `originalLine`, not `line`: GitHub nulls `line` exactly
# when a thread goes outdated (its anchored code changed), the case commit-driven
# resolution must catch, so the creation-side coordinate is the only one that
# survives (#125, ADR 0017). `has_fix_note` is true when a *daemon* comment in the
# thread carries the `_Fixed:_` note's hidden sentinel, so an already-noted thread
# skips re-judgment and routes to resolve-only retry (ADR 0017 §4). A comment
# counts only when it carries BOTH the Provenance tag and the sentinel, so an
# Operator quoting the sentinel into a reply cannot trip it; both literals mirror
# resolve_threads (PROVENANCE_MARKER and FIX_NOTE_SENTINEL), pinned by
# test_resolve_threads. Fetches comments(first:100) so a note posted as a later
# reply is visible (the sentinel is on the note, not the root). A thread past 100
# comments reads as un-noted: it is re-judged, and if its earlier resolve had
# dropped it could take a second note (one extra notification), the recoverable
# side. Best-effort: prints `[]` and returns non-zero on query failure, so the
# caller degrades to "no candidates".
fetch_open_review_threads() {
  local owner="$1" repo="$2" pr="$3"
  local resp
  # $owner/$repo/$pr are GraphQL variables, not shell vars — keep them literal.
  # shellcheck disable=SC2016
  if ! resp="$(gh api graphql \
    -f query='query($owner:String!,$repo:String!,$pr:Int!){
      repository(owner:$owner,name:$repo){
        pullRequest(number:$pr){
          reviewThreads(first:100){
            nodes{
              id isResolved path originalLine originalStartLine
              comments(first:100){ nodes{ author{ login } body } }
            }
          }
        }
      }
    }' -f owner="$owner" -f repo="$repo" -F pr="$pr" 2>/dev/null)"; then
    printf '[]'
    return 1
  fi
  jq -c '[.data.repository.pullRequest.reviewThreads.nodes[]
    | {
        thread_id: .id,
        is_resolved: .isResolved,
        root_author: (.comments.nodes[0].author.login // null),
        root_body: (.comments.nodes[0].body // ""),
        path: .path,
        original_line: .originalLine,
        original_start_line: .originalStartLine,
        has_fix_note: ([.comments.nodes[].body // "" | (contains("🤖 _pr-review-agent_") and contains("<!-- pr-review-agent:fixed -->"))] | any)
      }]' <<<"$resp"
}

# derive_project_identity <repo-root>
# Sets PROJECT_URL/PROJECT_NAME from `git remote get-url origin`. Returns
# non-zero if the origin is missing or not a parseable github.com URL.
# shellcheck disable=SC2034  # PROJECT_URL/NAME consumed by callers
derive_project_identity() {
  local repo_root="$1"
  local remote_url derived_owner derived_repo
  remote_url="$(git -C "$repo_root" remote get-url origin 2>/dev/null)" || remote_url=""
  # Constrain repo to no-slash + tolerate trailing slash; strip `.git` shell-side
  # (bash ERE lacks lazy `+?` so `(\.git)?` doesn't compose with greedy capture).
  if [[ "$remote_url" =~ github\.com[:/]([^/]+)/([^/]+)/?$ ]]; then
    derived_owner="${BASH_REMATCH[1]}"
    derived_repo="${BASH_REMATCH[2]%.git}"
  fi
  if [[ -z "${derived_owner:-}" || -z "${derived_repo:-}" ]]; then
    log_err "could not derive project identity — \`git -C $repo_root remote get-url origin\` did not return a parseable github.com URL"
    return 1
  fi
  PROJECT_URL="https://github.com/${derived_owner}/${derived_repo}"
  PROJECT_NAME="$derived_repo"
}

# Provenance tag on every posted artifact that is not a Review body — the
# Inline comment (create-review.sh), the Status comment (below), and the reply
# (create_reply.py's own MARKER). Single bash source: create-review.sh sources lib.sh
# and reads this. Answers "who wrote this", never draft-status (ADR 0010 §1).
# ADR 0010 §3 is the documentary source of truth: this constant and
# create_reply.py's MARKER each hard-code the string — a runtime shared constant
# across the bash/Python boundary was rejected there as costlier than a drift
# test — and test_provenance_tag.py pins the two definitions identical.
# shellcheck disable=SC2034  # also consumed by create-review.sh after sourcing
PROVENANCE_TAG='🤖 _pr-review-agent_'

# Marker identifying the agent's edit-in-place review-status comment (#60).
# find_status_comment keys on it to reuse the one comment across ticks rather
# than post a second. Distinct from the sha sentinel, which lives in the Review
# body and drives dedup; this marker never does.
STATUS_COMMENT_MARKER='<!-- pr-review-agent:status -->'

# status_sha_link <repo-url> <head-oid>
# Renders the head SHA as a short, backtick-wrapped markdown link to its commit
# page on the HEAD repo (the same repo the finding blob links target, so fork
# PRs resolve correctly). Display is the 12-char short SHA; the href uses the
# full SHA so GitHub resolves it unambiguously.
status_sha_link() {
  local repo_url="$1" head_oid="$2"
  # printf format is literal markdown; the values fill the %s (not shell expansion).
  # shellcheck disable=SC2016
  printf '[`%s`](%s/commit/%s)' "${head_oid:0:12}" "$repo_url" "$head_oid"
}

# status_scope_link <repo-url> <last-sha> <head-oid>
# Renders the diff scope: a backtick-wrapped, linked compare range when a prior
# review SHA is known (`<last>..<head>` display, /compare/<last>...<head> href —
# GitHub compare takes THREE dots between full SHAs), or the literal, unlinked
# `full PR` on a first review. Targets the HEAD repo for fork correctness.
status_scope_link() {
  local repo_url="$1" last_sha="$2" head_oid="$3"
  if [[ -z "$last_sha" ]]; then
    printf 'full PR'
  else
    # printf format is literal markdown; the values fill the %s (not shell expansion).
    # shellcheck disable=SC2016
    printf '[`%s..%s`](%s/compare/%s...%s)' \
      "${last_sha:0:12}" "${head_oid:0:12}" "$repo_url" "$last_sha" "$head_oid"
  fi
}

# render_status_comment <head-line> <scope-label> <file-count> <files>
# Assembles the status-comment body (#60): header, diff scope (commit range +
# file list, folded in <details> so a wide PR stays compact), and the marker
# find_status_comment keys on. Scope only — never the review findings, which
# would duplicate the Review object.
render_status_comment() {
  local head_line="$1" scope_label="$2" file_count="$3" files="$4"
  local noun="files"
  [[ "$file_count" == "1" ]] && noun="file"
  local bullets
  # sed script is literal (drop blank lines, wrap each path in `- ` … ``); the
  # `$` is sed's end-of-line, not a shell expansion.
  # shellcheck disable=SC2016
  bullets="$(printf '%s\n' "$files" | sed '/^[[:space:]]*$/d; s/^/- `/; s/$/`/')"
  # Visible Provenance tag (ADR 0010), then the hidden Status marker last.
  printf '%s\n\n_Scope: %s_\n\n<details><summary>%s %s</summary>\n\n%s\n\n</details>\n\n%s\n\n%s\n' \
    "$head_line" "$scope_label" "$file_count" "$noun" "$bullets" "$PROVENANCE_TAG" "$STATUS_COMMENT_MARKER"
}

# diff_paths <unified-diff-file>
# Prints the `b/` path of each `diff --git` header, one per line — the file
# list for the status-comment scope.
diff_paths() {
  local diff_file="$1"
  [[ -r "$diff_file" ]] || return 0
  sed -n 's|^diff --git a/.* b/||p' "$diff_file"
}

# find_status_comment <owner> <repo> <pr-number> <operator>
# Prints the id of the operator's status comment (the one carrying
# STATUS_COMMENT_MARKER) so a re-review edits it rather than posting a second
# (#60). Best-effort: returns 0 even on gh failure, falling back to a fresh
# post. `last` wins if more than one slipped through.
find_status_comment() {
  local owner="$1" repo="$2" pr="$3" operator="$4"
  [[ -n "$operator" ]] || return 0
  gh api "repos/${owner}/${repo}/issues/${pr}/comments" --paginate \
    --jq ".[] | select(.user.login == \"${operator}\") | select(.body | contains(\"${STATUS_COMMENT_MARKER}\")) | .id" \
    2>/dev/null | tail -1 || true
}

# post_status_comment <owner> <repo> <pr-number> <body>
# Posts a new issue comment and prints its id. Best-effort: prints nothing and
# returns 0 on failure, so a missing status comment never aborts the review.
post_status_comment() {
  local owner="$1" repo="$2" pr="$3" body="$4"
  gh api "repos/${owner}/${repo}/issues/${pr}/comments" \
    -f body="$body" --jq '.id' 2>/dev/null || true
}

# edit_status_comment <owner> <repo> <comment-id> <body>
# Edits an issue comment in place; a no-op on an empty id. Best-effort (returns
# 0 even on failure) — a failed status edit is not worth aborting a landed
# review over.
edit_status_comment() {
  local owner="$1" repo="$2" comment_id="$3" body="$4"
  [[ -n "$comment_id" ]] || return 0
  gh api -X PATCH "repos/${owner}/${repo}/issues/comments/${comment_id}" \
    -f body="$body" >/dev/null 2>&1 || true
}
