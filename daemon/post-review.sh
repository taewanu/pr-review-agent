#!/usr/bin/env bash
# post-review.sh — submit the assembled pending review to GitHub via gh api.
#
# Slice 1: inline comment bodies are "raw form" — "[severity] [type]\n\n{body}".
# Slice 3 swaps in severity emoji, bold type label, and an AI-drafted footer.
# Slice 2 introduces the `## Additional findings` section for unanchored
# findings; Slice 1 passes everything through as anchored, so unanchored.json is
# always empty and the section is omitted.

set -euo pipefail

# shellcheck source=daemon/lib.sh
source "$(dirname "$0")/lib.sh"

DRY_RUN=0
HEAD_SHA=""
OWNER=""
REPO=""
NUMBER=""
SUMMARY_FILE=""
ANCHORED=""
UNANCHORED=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --owner)
      OWNER="$2"
      shift 2
      ;;
    --repo)
      REPO="$2"
      shift 2
      ;;
    --number)
      NUMBER="$2"
      shift 2
      ;;
    --summary-file)
      SUMMARY_FILE="$2"
      shift 2
      ;;
    --anchored)
      ANCHORED="$2"
      shift 2
      ;;
    --unanchored)
      UNANCHORED="$2"
      shift 2
      ;;
    --head-sha)
      HEAD_SHA="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *)
      log_err "post-review.sh: unknown arg: $1"
      exit 1
      ;;
  esac
done

[[ -n "$OWNER" ]] || {
  log_err "missing --owner"
  exit 1
}
[[ -n "$REPO" ]] || {
  log_err "missing --repo"
  exit 1
}
[[ -n "$NUMBER" ]] || {
  log_err "missing --number"
  exit 1
}
[[ -n "$SUMMARY_FILE" ]] || {
  log_err "missing --summary-file"
  exit 1
}
[[ -n "$ANCHORED" ]] || {
  log_err "missing --anchored"
  exit 1
}
[[ -n "$UNANCHORED" ]] || {
  log_err "missing --unanchored"
  exit 1
}

summary="$(cat "$SUMMARY_FILE")"

comments_json="$(jq '
  map({
    path: .path,
    line: .line,
    side: "RIGHT",
    body: ("[" + .severity + "] [" + .type + "]\n\n" + .body)
  })
' "$ANCHORED")"

payload="$(jq -n \
  --arg body "$summary" \
  --argjson comments "$comments_json" \
  --arg commit_id "$HEAD_SHA" \
  '{
    body: $body,
    comments: $comments
  } + (if $commit_id == "" then {} else {commit_id: $commit_id} end)')"

if [[ $DRY_RUN -eq 1 ]]; then
  printf '%s\n' "$payload"
  exit 0
fi

log_info "posting Pending review to ${OWNER}/${REPO}#${NUMBER}"
printf '%s' "$payload" | gh api \
  --method POST \
  "repos/${OWNER}/${REPO}/pulls/${NUMBER}/reviews" \
  --input -
