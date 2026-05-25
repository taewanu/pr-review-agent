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
# Resolves the project URL and display name for the footer/banner from, in
# order: PR_REVIEW_PROJECT_URL/NAME env vars (operator override), then the
# git origin remote of <repo-root>. Sets PROJECT_URL and PROJECT_NAME globals.
# Returns non-zero with an actionable error if neither source yields values
# (rare: tarball install with no git remote and no env vars).
derive_project_identity() {
  local repo_root="$1"
  local remote_url="" derived_owner="" derived_repo=""
  remote_url="$(git -C "$repo_root" remote get-url origin 2>/dev/null)" || true
  if [[ "$remote_url" =~ github\.com[:/]([^/]+)/([^/.]+)(\.git)?$ ]]; then
    derived_owner="${BASH_REMATCH[1]}"
    derived_repo="${BASH_REMATCH[2]}"
  fi
  PROJECT_URL="${PR_REVIEW_PROJECT_URL:-${derived_owner:+https://github.com/${derived_owner}/${derived_repo}}}"
  PROJECT_NAME="${PR_REVIEW_PROJECT_NAME:-$derived_repo}"
  if [[ -z "$PROJECT_URL" || -z "$PROJECT_NAME" ]]; then
    log_err "could not derive project identity from git remote at $repo_root; set PR_REVIEW_PROJECT_URL and PR_REVIEW_PROJECT_NAME manually"
    return 1
  fi
}
