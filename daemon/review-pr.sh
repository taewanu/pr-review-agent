#!/usr/bin/env bash
# review-pr.sh — process a single PR end-to-end: scratch clone, run claude,
# extract + anchor the structured payload, post the result as a Pending review.
#
# Usage:
#   bash daemon/review-pr.sh [--keep-scratch] <pr-url>

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
# Fail before the expensive claude call if project identity can't be derived.
# post-review.sh re-derives at post time, but catching it here saves 2-3 min
# of wasted work per tick.
derive_project_identity "$SCRIPT_DIR/.."

KEEP_SCRATCH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-scratch)
      KEEP_SCRATCH=1
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

if [[ $# -ne 1 ]]; then
  log_err "usage: review-pr.sh [--keep-scratch] <pr-url>"
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

# Set after `gh pr view`; leave blank so log_failure pre-view still has the
# placeholder field populated.
HEAD_OID=""

# Parse the `category=<slug>` first stderr line emitted by extract-json.py and
# post-review.sh on failure. Falls back to `unknown` so the structured failure
# line is always populated.
extract_category() {
  local stderr_path="$1"
  local cat
  cat="$(grep -m1 '^category=' "$stderr_path" 2>/dev/null | cut -d= -f2 || true)"
  [[ -n "$cat" ]] && printf '%s' "$cat" || printf 'unknown'
}

log_info "PR ${BASE_OWNER}/${BASE_REPO}#${PR_NUMBER}"

meta="$(gh pr view "$PR_URL" --json headRepository,headRepositoryOwner,headRefName,headRefOid)"
HEAD_REPO_OWNER="$(jq -r '.headRepositoryOwner.login // empty' <<<"$meta")"
HEAD_REPO_NAME="$(jq -r '.headRepository.name // empty' <<<"$meta")"
HEAD_REF="$(jq -r '.headRefName // empty' <<<"$meta")"
HEAD_OID="$(jq -r '.headRefOid // empty' <<<"$meta")"
if [[ -z "$HEAD_REPO_OWNER" || -z "$HEAD_REPO_NAME" || -z "$HEAD_REF" || -z "$HEAD_OID" ]]; then
  log_err "gh pr view returned incomplete metadata for $PR_URL (closed PR with deleted fork?)"
  exit 1
fi
HEAD_REPO="${HEAD_REPO_OWNER}/${HEAD_REPO_NAME}"
log_info "head: ${HEAD_REPO}@${HEAD_REF} (${HEAD_OID:0:12})"

SCRATCH="$(mktemp -d -t pr-review-agent.XXXXXX)"
if [[ $KEEP_SCRATCH -eq 1 ]]; then
  log_info "scratch (will be preserved): $SCRATCH"
else
  trap 'rm -rf "$SCRATCH"' EXIT
  log_info "scratch: $SCRATCH"
fi

gh repo clone "$HEAD_REPO" "$SCRATCH" -- --quiet --depth=1 --no-tags
(
  cd "$SCRATCH"
  # PR refs are server-side stable even after the head branch is deleted or
  # squash-merged into an unreachable SHA. The `--branch $HEAD_REF` shortcut
  # silently breaks on merged PRs whose branch has since been deleted.
  git fetch --quiet --depth=1 origin "refs/pull/${PR_NUMBER}/head"
  git checkout --quiet --detach "$HEAD_OID"
)

# Bare filenames inside the scratch dir. The claude prompt below references the
# diff by basename so $TMPDIR containing a space can't split the slash-command args.
DIFF_BASENAME=".pr-review-diff.txt"
DIFF_FILE="$SCRATCH/$DIFF_BASENAME"
RAW_FILE="$SCRATCH/.pr-review-raw.txt"
PAYLOAD_FILE="$SCRATCH/.pr-review-payload.json"
ANCHORED_FILE="$SCRATCH/.pr-review-anchored.json"
UNANCHORED_FILE="$SCRATCH/.pr-review-unanchored.json"
SUMMARY_FILE="$SCRATCH/.pr-review-summary.txt"
EXTRACT_ERR="$SCRATCH/.pr-review-extract.err"
ANCHOR_OUT="$SCRATCH/.pr-review-anchor.out"
POST_ERR="$SCRATCH/.pr-review-post.err"

log_step "fetching diff"
gh pr diff "$PR_URL" >"$DIFF_FILE"

log_step "running review agent via claude -p"
(
  cd "$SCRATCH"
  claude -p "/review-pr $PR_URL --diff $DIFF_BASENAME" >"$RAW_FILE"
)
if [[ ! -s "$RAW_FILE" ]]; then
  log_failure "empty-stdout" "$PR_URL" "$HEAD_OID" "claude produced no output"
  exit 1
fi

log_step "extracting payload"
if ! python3 "$SCRIPT_DIR/extract-json.py" "$RAW_FILE" >"$PAYLOAD_FILE" 2>"$EXTRACT_ERR"; then
  cat "$EXTRACT_ERR" >&2
  log_failure "$(extract_category "$EXTRACT_ERR")" "$PR_URL" "$HEAD_OID" "extract-json.py exited non-zero"
  exit 1
fi

log_step "anchoring findings"
python3 "$SCRIPT_DIR/anchor-findings.py" \
  "$PAYLOAD_FILE" "$DIFF_FILE" \
  --anchored "$ANCHORED_FILE" \
  --unanchored "$UNANCHORED_FILE" \
  >"$ANCHOR_OUT"
DROPPED_COMBO="$(grep -m1 '^dropped_forbidden_combo=' "$ANCHOR_OUT" | cut -d= -f2 || true)"
DROPPED_COMBO="${DROPPED_COMBO:-0}"

jq -r '.summary' "$PAYLOAD_FILE" >"$SUMMARY_FILE"

log_step "posting Pending review"
if ! bash "$SCRIPT_DIR/post-review.sh" \
  --owner "$BASE_OWNER" \
  --repo "$BASE_REPO" \
  --number "$PR_NUMBER" \
  --head-sha "$HEAD_OID" \
  --summary-file "$SUMMARY_FILE" \
  --anchored "$ANCHORED_FILE" \
  --unanchored "$UNANCHORED_FILE" \
  --dropped-combo "$DROPPED_COMBO" \
  2>"$POST_ERR"; then
  cat "$POST_ERR" >&2
  category="$(extract_category "$POST_ERR")"
  reason="gh api POST failed"
  if [[ "$category" == "pending-conflict" ]]; then
    reason="existing pending review on PR — submit or cancel via UI before re-running"
  fi
  log_failure "$category" "$PR_URL" "$HEAD_OID" "$reason"
  exit 1
fi

log_step "done"
