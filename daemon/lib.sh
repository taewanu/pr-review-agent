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

# derive_project_identity <repo-root>
# Sets PROJECT_URL and PROJECT_NAME by parsing `git remote get-url origin` of
# <repo-root>. Any fork running from a normal git clone gets correct identity
# with zero config — canonical clone advertises itself; fork clone advertises
# itself. Returns non-zero with an actionable error if the origin is missing
# or not a parseable github.com URL.
#
# Greedy match (no lazy `+?` — POSIX ERE on macOS doesn't support it) captures
# everything after `owner/`, then the `%.git` suffix-strip drops the optional
# `.git`. This keeps dots in real repo names like `chartjs/Chart.js`.
# shellcheck disable=SC2034  # PROJECT_URL/NAME are consumed by callers after sourcing lib.sh
derive_project_identity() {
  local repo_root="$1"
  local remote_url derived_owner derived_repo
  remote_url="$(git -C "$repo_root" remote get-url origin 2>/dev/null)" || remote_url=""
  if [[ "$remote_url" =~ github\.com[:/]([^/]+)/(.+)$ ]]; then
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
