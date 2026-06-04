#!/usr/bin/env bash
# submit-review.sh — submit the operator's pending review on a PR via the GitHub
# API, preserving the drafted body. The web "Finish your review" modal blanks the
# body when its textarea is left empty (#50); the events API path never touches
# the modal, so the drafted summary survives. Per ADR 0008 this is the submit
# path for others' PRs (own PRs auto-submit at review time, no pending stage).
#
# Usage:
#   bash daemon/submit-review.sh [--event COMMENT|APPROVE|REQUEST_CHANGES] [--dry-run] <pr-url>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=daemon/lib.sh disable=SC1091
source "$SCRIPT_DIR/lib.sh"

for cmd in gh jq; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log_err "missing '$cmd' on PATH"
    exit 1
  fi
done
if ! gh auth status >/dev/null 2>&1; then
  log_err "gh not authenticated — run 'gh auth login' first"
  exit 1
fi

EVENT="COMMENT"
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --event)
      if [[ $# -lt 2 ]]; then
        log_err "--event requires a value"
        exit 1
      fi
      EVENT="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
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

case "$EVENT" in
  COMMENT | APPROVE | REQUEST_CHANGES) ;;
  *)
    log_err "--event must be COMMENT, APPROVE, or REQUEST_CHANGES (got: $EVENT)"
    exit 1
    ;;
esac

if [[ $# -ne 1 ]]; then
  log_err "usage: submit-review.sh [--event COMMENT|APPROVE|REQUEST_CHANGES] [--dry-run] <pr-url>"
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

log_info "looking for a pending review by ${GITHUB_USER} on ${OWNER}/${REPO}#${PR_NUMBER}"

reviews_json="$(gh api --paginate "repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/reviews")"
# GitHub allows one pending review per user per PR; `last` is defensive.
review_id="$(jq -r --arg login "$GITHUB_USER" '
  [.[] | select(.state == "PENDING" and .user.login == $login)] | last | .id // empty
' <<<"$reviews_json")"

if [[ -z "$review_id" ]]; then
  log_info "no pending review to submit (own PRs auto-submit at review time, so nothing is drafted here)"
  exit 0
fi

endpoint="repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/reviews/${review_id}/events"

if [[ $DRY_RUN -eq 1 ]]; then
  jq -n --argjson review_id "$review_id" --arg event "$EVENT" --arg endpoint "$endpoint" \
    '{review_id: $review_id, event: $event, endpoint: $endpoint}'
  exit 0
fi

# Submit via the events API, omitting `body` so the drafted pending body
# survives. The web "Finish your review" modal blanks it when left empty (#50);
# this path never touches the modal.
log_info "submitting review ${review_id} as ${EVENT}"
if ! gh api -X POST "$endpoint" -f event="$EVENT"; then
  log_err "submit failed for review ${review_id} on ${OWNER}/${REPO}#${PR_NUMBER}"
  exit 1
fi
log_info "submitted"
