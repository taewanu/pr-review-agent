#!/usr/bin/env bash
# review-pr.sh — process a single PR end-to-end: scratch clone, run claude,
# extract + anchor the structured payload, post the result as a Pending review.
#
# Slice 1 scope: happy-path orchestration. Failure categorization (ADR 0005),
# real diff anchoring (Slice 2), and rich rendering (Slice 3) land later.
#
# Usage:
#   bash daemon/review-pr.sh <pr-url>

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

if [[ $# -ne 1 ]]; then
  log_err "usage: review-pr.sh <pr-url>"
  exit 1
fi

PR_URL="$1"

if [[ ! "$PR_URL" =~ ^https://github\.com/([^/]+)/([^/]+)/pull/([0-9]+) ]]; then
  log_err "invalid PR URL: $PR_URL"
  exit 1
fi
BASE_OWNER="${BASH_REMATCH[1]}"
BASE_REPO="${BASH_REMATCH[2]}"
PR_NUMBER="${BASH_REMATCH[3]}"

log_info "PR ${BASE_OWNER}/${BASE_REPO}#${PR_NUMBER}"

meta="$(gh pr view "$PR_URL" --json headRepository,headRepositoryOwner,headRefName,headRefOid)"
HEAD_REPO="$(jq -r '.headRepositoryOwner.login + "/" + .headRepository.name' <<<"$meta")"
HEAD_REF="$(jq -r '.headRefName' <<<"$meta")"
HEAD_OID="$(jq -r '.headRefOid' <<<"$meta")"
if [[ "$HEAD_REPO" == "/"* || "$HEAD_REPO" == */"" ]]; then
  log_err "could not resolve head repo from gh pr view output: '$HEAD_REPO'"
  exit 1
fi
log_info "head: ${HEAD_REPO}@${HEAD_REF} (${HEAD_OID:0:12})"

SCRATCH="$(mktemp -d -t pr-review-agent.XXXXXX)"
trap 'rm -rf "$SCRATCH"' EXIT
log_info "scratch: $SCRATCH"

gh repo clone "$HEAD_REPO" "$SCRATCH" -- \
  --quiet --depth=1 --no-tags --branch "$HEAD_REF"
(
  cd "$SCRATCH"
  # Branch tip may have moved since the PR's HEAD; fetch the exact commit if needed.
  if ! git cat-file -e "$HEAD_OID" 2>/dev/null; then
    git fetch --quiet --depth=1 origin "$HEAD_OID"
  fi
  git checkout --quiet --detach "$HEAD_OID"
)

DIFF_FILE="$SCRATCH/.pr-review-diff.txt"
RAW_FILE="$SCRATCH/.pr-review-raw.txt"
PAYLOAD_FILE="$SCRATCH/.pr-review-payload.json"
ANCHORED_FILE="$SCRATCH/.pr-review-anchored.json"
UNANCHORED_FILE="$SCRATCH/.pr-review-unanchored.json"
SUMMARY_FILE="$SCRATCH/.pr-review-summary.txt"

log_info "fetching diff"
gh pr diff "$PR_URL" >"$DIFF_FILE"

log_info "running review agent via claude -p"
(
  cd "$SCRATCH"
  claude -p "/review-pr $PR_URL --diff $DIFF_FILE" >"$RAW_FILE"
)

log_info "extracting payload"
python3 "$SCRIPT_DIR/extract-json.py" "$RAW_FILE" >"$PAYLOAD_FILE"

log_info "anchoring findings"
python3 "$SCRIPT_DIR/anchor-findings.py" \
  "$PAYLOAD_FILE" "$DIFF_FILE" \
  --anchored "$ANCHORED_FILE" \
  --unanchored "$UNANCHORED_FILE"

jq -r '.summary' "$PAYLOAD_FILE" >"$SUMMARY_FILE"

log_info "posting Pending review"
"$SCRIPT_DIR/post-review.sh" \
  --owner "$BASE_OWNER" \
  --repo "$BASE_REPO" \
  --number "$PR_NUMBER" \
  --head-sha "$HEAD_OID" \
  --summary-file "$SUMMARY_FILE" \
  --anchored "$ANCHORED_FILE" \
  --unanchored "$UNANCHORED_FILE"

log_info "done"
