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

# State tracking for same-SHA dedup. One file per PR. Layered behind the
# sentinel-based dedup (ADR 0006) as a fallback when GitHub's reviews API is
# unavailable.
#
# Override $PR_REVIEW_STATE_DIR for tests.
_state_dir() {
  printf '%s' "${PR_REVIEW_STATE_DIR:-$HOME/.local/state/pr-review-agent}"
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
  for f in "$repo_root/.claude/commands/review-pr.md" "$repo_root/.claude/commands/reply-pr.md"; do
    [[ -e "$f" ]] || continue
    base="$(basename "$f")"
    [[ -e "$scratch/.claude/commands/$base" ]] || cp "$f" "$scratch/.claude/commands/$base"
  done
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

# post_pickup_ack <owner> <repo> <pr-number> <head-sha>
# Posts a transient "reviewing" PR comment (#48) so the operator has a PR-side
# signal during the multi-minute review, and prints the new comment's id on
# stdout. Best-effort: on any failure it prints nothing and still returns 0, so
# a missing ack never aborts the review.
post_pickup_ack() {
  local owner="$1" repo="$2" pr="$3" sha="$4"
  local body="👀 Reviewing \`${sha:0:12}\`… drafting a pending review."
  gh api "repos/${owner}/${repo}/issues/${pr}/comments" \
    -f body="$body" --jq '.id' 2>/dev/null || true
}

# delete_comment <owner> <repo> <comment-id>
# Deletes an issue comment by id; a no-op on an empty id. Best-effort (returns 0
# even when the delete fails) — used to clear the pickup ack (#48) once the
# review lands, where a failed cleanup is not worth aborting over.
delete_comment() {
  local owner="$1" repo="$2" comment_id="$3"
  [[ -n "$comment_id" ]] || return 0
  gh api -X DELETE "repos/${owner}/${repo}/issues/comments/${comment_id}" >/dev/null 2>&1 || true
}
