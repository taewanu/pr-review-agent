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

# load_env_file <path>
# Loads KEY=value lines from the file into the process env. Shell-env-wins:
# already-set variables are preserved so an inline `VAR=val …` invocation
# overrides .env for one-off testing. Quietly returns on unreadable or empty
# files so tests can disable via PR_REVIEW_ENV_FILE=/dev/null and a missing
# .env is not an error.
load_env_file() {
  local env_file="$1"
  [[ -r "$env_file" ]] || return 0
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    [[ -n "${!key:-}" ]] && continue
    export "$key=$value"
  done <"$env_file"
}
