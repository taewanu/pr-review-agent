#!/usr/bin/env bash
# post-review.sh — submit the assembled pending review to GitHub via gh api.

set -euo pipefail

# shellcheck source=daemon/lib.sh disable=SC1091
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

# Render unanchored findings into a Markdown section appended to the review body.
# `## Additional findings` is the canonical relocation surface per ADR 0005.
additional="$(jq -r '
  if length == 0 then ""
  else "\n\n## Additional findings\n\n" + (
    map(
      "- **" + .path + ":" + (.line | tostring) +
      (if .end_line and .end_line != .line then "-" + (.end_line | tostring) else "" end) +
      "** [" + .severity + "] [" + .type + "] — " + .body
    ) | join("\n")
  )
  end
' "$UNANCHORED")"

body_with_additional="${summary}${additional}"

# Build inline comment payloads. Range findings (end_line > line) use
# {start_line, start_side, line, side, body}; single-line uses {line, side, body}.
comments_json="$(jq '
  map(
    . as $f |
    {
      path: .path,
      side: "RIGHT",
      body: ("[" + .severity + "] [" + .type + "]\n\n" + .body)
    }
    + (
      if .end_line and .end_line > .line then
        {start_line: .line, start_side: "RIGHT", line: .end_line}
      else
        {line: .line}
      end
    )
  )
' "$ANCHORED")"

payload="$(jq -n \
  --arg body "$body_with_additional" \
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
