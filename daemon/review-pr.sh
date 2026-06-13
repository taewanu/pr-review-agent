#!/usr/bin/env bash
# review-pr.sh — process a single PR end-to-end: scratch clone, run claude,
# extract + anchor the structured payload, post the result as a Pending review.
#
# Usage:
#   bash daemon/review-pr.sh [--keep-scratch] [--last-sha <sha>] <pr-url>

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
# create-review.sh re-derives at post time, but catching it here saves 2-3 min
# of wasted work per tick. The PROJECT_URL/NAME globals set here are unused;
# create-review.sh's later call is the authoritative one.
derive_project_identity "$SCRIPT_DIR/.."

KEEP_SCRATCH=0
LAST_SHA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-scratch)
      KEEP_SCRATCH=1
      shift
      ;;
    --last-sha)
      if [[ $# -lt 2 ]]; then
        log_err "--last-sha requires a value"
        exit 1
      fi
      LAST_SHA="$2"
      shift 2
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
  log_err "usage: review-pr.sh [--keep-scratch] [--last-sha <sha>] <pr-url>"
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

# Durable edit-in-place status comment id (#60); set once posted/reused, edited
# in place — never deleted, so it outlives the run. Not touched by cleanup().
STATUS_COMMENT_ID=""

# Per-PR lock path (#67); set once acquired, released by cleanup().
LOCK_FILE=""

# Single EXIT path for the run-scoped artifacts: the per-PR lock (#67) and the
# scratch clone, neither of which should outlive the run. The status comment
# (#60) is deliberately durable, so it is not cleaned up here. The globals it
# reads default to empty, so it no-ops cleanly if the run dies before they're set.
cleanup() {
  release_pr_lock "${LOCK_FILE:-}"
  if [[ $KEEP_SCRATCH -eq 0 && -n "${SCRATCH:-}" ]]; then
    rm -rf "$SCRATCH"
  fi
}

# Parse the `category=<slug>` first stderr line emitted by extract_json.py and
# create-review.sh on failure. Falls back to `unknown` so the structured failure
# line is always populated.
extract_category() {
  local stderr_path="$1"
  local cat
  cat="$(grep -m1 '^category=' "$stderr_path" 2>/dev/null | cut -d= -f2 || true)"
  [[ -n "$cat" ]] && printf '%s' "$cat" || printf 'unknown'
}

log_info "PR ${BASE_OWNER}/${BASE_REPO}#${PR_NUMBER}"

meta="$(gh pr view "$PR_URL" --json headRepository,headRepositoryOwner,headRefName,headRefOid,author)"
HEAD_REPO_OWNER="$(jq -r '.headRepositoryOwner.login // empty' <<<"$meta")"
HEAD_REPO_NAME="$(jq -r '.headRepository.name // empty' <<<"$meta")"
HEAD_REF="$(jq -r '.headRefName // empty' <<<"$meta")"
HEAD_OID="$(jq -r '.headRefOid // empty' <<<"$meta")"
PR_AUTHOR="$(jq -r '.author.login // empty' <<<"$meta")"
if [[ -z "$HEAD_REPO_OWNER" || -z "$HEAD_REPO_NAME" || -z "$HEAD_REF" || -z "$HEAD_OID" ]]; then
  log_err "gh pr view returned incomplete metadata for $PR_URL (closed PR with deleted fork?)"
  exit 1
fi
HEAD_REPO="${HEAD_REPO_OWNER}/${HEAD_REPO_NAME}"
# Base URL for status-comment SHA/scope links (#102). Target the HEAD repo where
# HEAD_OID lives so links resolve on fork PRs, the same rule the finding blob
# links follow. Same-repo PRs have HEAD_REPO == BASE, so this still points home.
HEAD_REPO_URL="https://github.com/${HEAD_REPO}"
log_info "head: ${HEAD_REPO}@${HEAD_REF} (${HEAD_OID:0:12})"

# Own-vs-others gates the submit path (ADR 0008): own PRs auto-submit a COMMENT
# review, others' stay pending. The operator is the gh-authenticated identity
# (ADR 0003), as in reply-pr.sh. Derived here, not passed from poll.sh, so the
# manual one-shot is correct too; a blank author falls through to the others' path.
OPERATOR="$(gh api user --jq '.login' 2>/dev/null || true)"
OWN_PR=0
if [[ -n "$PR_AUTHOR" && "$PR_AUTHOR" == "$OPERATOR" ]]; then
  OWN_PR=1
  log_info "own PR (author == operator '$OPERATOR'): auto-submitting a COMMENT review"
fi

# Take the per-PR lock before any work, skipping if a review of this PR is
# already in flight (#67; rationale on acquire_pr_lock). The EXIT trap moves up
# to here so the lock is released on every exit below, including the dedup skip;
# cleanup() no-ops on the still-empty ack/scratch globals.
if ! LOCK_FILE="$(acquire_pr_lock "$BASE_OWNER" "$BASE_REPO" "$PR_NUMBER")"; then
  log_info "review already in progress for ${BASE_OWNER}/${BASE_REPO}#${PR_NUMBER}, skipping"
  exit 0
fi
trap cleanup EXIT

# Idempotency for the sequential case: skip if the operator already reviewed this
# exact HEAD (the lock above covers the concurrent case). poll.sh dedups before
# dispatch, but the manual one-shot bypasses that. A discovery failure falls
# through to reviewing rather than skipping on uncertainty.
if existing_sha="$(discover_sentinel_sha "$BASE_OWNER" "$BASE_REPO" "$PR_NUMBER" "$OPERATOR")" &&
  [[ "$existing_sha" == "$HEAD_OID" ]]; then
  log_info "already reviewed ${HEAD_OID:0:12}, skipping"
  exit 0
fi

SCRATCH="$(mktemp -d -t pr-review-agent.XXXXXX)"
if [[ $KEEP_SCRATCH -eq 1 ]]; then
  log_info "scratch (will be preserved): $SCRATCH"
else
  log_info "scratch: $SCRATCH"
fi

# Bound the https clone/fetch so a stalled connection aborts cleanly instead of
# hanging the loop (#121). Backstopped by poll.sh's per-PR watchdog.
arm_git_stall_timeout
gh repo clone "$HEAD_REPO" "$SCRATCH" -- --quiet --depth=1 --no-tags
(
  cd "$SCRATCH"
  # PR refs are server-side stable even after the head branch is deleted or
  # squash-merged into an unreachable SHA. The `--branch $HEAD_REF` shortcut
  # silently breaks on merged PRs whose branch has since been deleted.
  git fetch --quiet --depth=1 origin "refs/pull/${PR_NUMBER}/head"
  git checkout --quiet --detach "$HEAD_OID"
)

# Bundle operator's agent + slash-command defs into the scratch so claude -p
# loads them from cwd without requiring target-repo .claude/ setup (ADR 0007).
bundle_operator_agents "$SCRATCH"

# Bare filenames inside the scratch dir. The claude prompt below references the
# diff by basename so $TMPDIR containing a space can't split the slash-command args.
DIFF_BASENAME=".pr-review-diff.txt"
DIFF_FILE="$SCRATCH/$DIFF_BASENAME"
RAW_FILE="$SCRATCH/.pr-review-raw.txt"
# The editor agent reads the draft from cwd by basename (like the diff), so the
# author payload lives in the scratch under a bare name (#133).
AUTHOR_BASENAME=".pr-review-author.json"
AUTHOR_FILE="$SCRATCH/$AUTHOR_BASENAME"
EDIT_RAW_FILE="$SCRATCH/.pr-review-edit-raw.txt"
PAYLOAD_FILE="$SCRATCH/.pr-review-payload.json"
ANCHORED_FILE="$SCRATCH/.pr-review-anchored.json"
UNANCHORED_FILE="$SCRATCH/.pr-review-unanchored.json"
SUMMARY_FILE="$SCRATCH/.pr-review-summary.txt"
EXTRACT_ERR="$SCRATCH/.pr-review-extract.err"
EDIT_ERR="$SCRATCH/.pr-review-edit.err"
ANCHOR_OUT="$SCRATCH/.pr-review-anchor.out"
POST_ERR="$SCRATCH/.pr-review-post.err"

log_step "fetching diff"
# When --last-sha is set, scope the diff to changes since the prior review's
# HEAD so the agent only re-reads what's new. Falls back to the full PR diff
# when last_sha wasn't passed (first-review), can't be fetched, or is no longer
# an ancestor of HEAD after a force-push/rebase (#123): an incremental diff
# across diverged tips reads the wrong delta. git supports `fetch origin <sha>`
# against GitHub when the SHA is reachable from any ref the server exposes.
diff_scoped=0
if [[ -n "$LAST_SHA" ]]; then
  if (cd "$SCRATCH" && git fetch --quiet origin "$LAST_SHA" 2>/dev/null); then
    if is_fast_forward "$SCRATCH" "$LAST_SHA"; then
      (cd "$SCRATCH" && git diff "$LAST_SHA..HEAD") >"$DIFF_FILE"
      diff_scoped=1
      log_info "diff scoped to ${LAST_SHA:0:12}..HEAD"
    else
      log_info "non-fast-forward since ${LAST_SHA:0:12} (force-push/rebase), using full PR diff"
    fi
  else
    log_info "could not fetch ${LAST_SHA:0:12}, falling back to full PR diff"
  fi
fi
if [[ $diff_scoped -eq 0 ]]; then
  gh pr diff "$PR_URL" >"$DIFF_FILE"
fi

# Post (or reuse) the durable status comment before the multi-minute review, so
# the operator sees the PR is being looked at and the scope being read (#60).
# Scope comes from the same diff: file list plus commit range (full PR first,
# <last-sha>..HEAD on re-review). One comment per PR, reused across ticks.
STATUS_FILES="$(diff_paths "$DIFF_FILE")"
STATUS_FILE_COUNT="$(printf '%s' "$STATUS_FILES" | grep -c . || true)"
# Scope links to the HEAD-repo compare range on a re-review, or stays the
# literal `full PR` on a first review (#102). Pass LAST_SHA only when the diff
# was actually scoped to it (a fetch failure falls back to the full diff).
if [[ $diff_scoped -eq 1 ]]; then
  STATUS_SCOPE="$(status_scope_link "$HEAD_REPO_URL" "$LAST_SHA" "$HEAD_OID")"
else
  STATUS_SCOPE="$(status_scope_link "$HEAD_REPO_URL" "" "$HEAD_OID")"
fi
reviewing_body="$(render_status_comment \
  "👀 Reviewing $(status_sha_link "$HEAD_REPO_URL" "$HEAD_OID")…" \
  "$STATUS_SCOPE" "$STATUS_FILE_COUNT" "$STATUS_FILES")"
STATUS_COMMENT_ID="$(find_status_comment "$BASE_OWNER" "$BASE_REPO" "$PR_NUMBER" "$OPERATOR")"
if [[ -n "$STATUS_COMMENT_ID" ]]; then
  edit_status_comment "$BASE_OWNER" "$BASE_REPO" "$STATUS_COMMENT_ID" "$reviewing_body"
  log_info "status comment reused (${STATUS_COMMENT_ID})"
else
  STATUS_COMMENT_ID="$(post_status_comment "$BASE_OWNER" "$BASE_REPO" "$PR_NUMBER" "$reviewing_body")"
  if [[ -n "$STATUS_COMMENT_ID" ]]; then
    log_info "status comment posted (${STATUS_COMMENT_ID})"
  else
    log_info "status comment unavailable (non-fatal)"
  fi
fi

log_step "running review agent via claude -p"
# Wall-clock backstop (#76), symmetric with the reply agent: same unbounded
# `claude -p` shape, same 300s rationale (see reply-pr.sh). Partial output on
# timeout is discarded, not parsed.
REVIEW_AGENT_TIMEOUT="${REVIEW_AGENT_TIMEOUT:-300}"
review_rc=0
(
  cd "$SCRATCH"
  run_with_timeout "$REVIEW_AGENT_TIMEOUT" \
    claude -p "/review-pr $PR_URL --diff $DIFF_BASENAME" >"$RAW_FILE"
) || review_rc=$?
if [[ "$review_rc" -eq "$TIMEOUT_EXIT" ]]; then
  log_failure "review-timeout" "$PR_URL" "$HEAD_OID" \
    "review agent exceeded ${REVIEW_AGENT_TIMEOUT}s"
  exit 1
fi
if [[ ! -s "$RAW_FILE" ]]; then
  log_failure "empty-stdout" "$PR_URL" "$HEAD_OID" "claude produced no output"
  exit 1
fi

log_step "extracting payload"
# --no-style: the voice gate moved behind the editor (ADR 0016). This parse only
# schema-validates the author draft and shapes it to hand to the editor; the
# final gate runs in apply_edits.py, on what is posted.
if ! python3 "$SCRIPT_DIR/extract_json.py" --no-style "$RAW_FILE" >"$AUTHOR_FILE" 2>"$EXTRACT_ERR"; then
  cat "$EXTRACT_ERR" >&2
  log_failure "$(extract_category "$EXTRACT_ERR")" "$PR_URL" "$HEAD_OID" "extract_json.py exited non-zero"
  exit 1
fi

# Editorial pass (#133, ADR 0016): a fresh editor agent re-reads the PR at HEAD
# and refines the draft (drop weak findings, sharpen survivors, reconcile the
# summary) before posting. Skipped on a zero-finding draft, where there is
# nothing to refine; apply_edits.py still runs the moved voice gate either way.
EDIT_ARGS=(--author "$AUTHOR_FILE")
if [[ "$(jq '.comments | length' "$AUTHOR_FILE")" -gt 0 ]]; then
  log_step "running editor agent via claude -p"
  # Same unbounded `claude -p` shape and 300s backstop as the review agent.
  EDITOR_AGENT_TIMEOUT="${EDITOR_AGENT_TIMEOUT:-300}"
  edit_rc=0
  (
    cd "$SCRATCH"
    run_with_timeout "$EDITOR_AGENT_TIMEOUT" \
      claude -p "/edit-review $PR_URL --diff $DIFF_BASENAME --payload $AUTHOR_BASENAME" >"$EDIT_RAW_FILE"
  ) || edit_rc=$?
  if [[ "$edit_rc" -eq "$TIMEOUT_EXIT" ]]; then
    log_failure "edit-timeout" "$PR_URL" "$HEAD_OID" "editor agent exceeded ${EDITOR_AGENT_TIMEOUT}s"
    exit 1
  fi
  if [[ ! -s "$EDIT_RAW_FILE" ]]; then
    log_failure "edit-empty" "$PR_URL" "$HEAD_OID" "editor produced no output"
    exit 1
  fi
  EDIT_ARGS+=(--edits "$EDIT_RAW_FILE")
fi

log_step "applying edits"
if ! python3 "$SCRIPT_DIR/apply_edits.py" "${EDIT_ARGS[@]}" >"$PAYLOAD_FILE" 2>"$EDIT_ERR"; then
  cat "$EDIT_ERR" >&2
  log_failure "$(extract_category "$EDIT_ERR")" "$PR_URL" "$HEAD_OID" "apply_edits.py exited non-zero"
  exit 1
fi

log_step "anchoring findings"
python3 "$SCRIPT_DIR/anchor_findings.py" \
  "$PAYLOAD_FILE" "$DIFF_FILE" \
  --anchored "$ANCHORED_FILE" \
  --unanchored "$UNANCHORED_FILE" \
  >"$ANCHOR_OUT"
DROPPED_COMBO="$(grep -m1 '^dropped_forbidden_combo=' "$ANCHOR_OUT" | cut -d= -f2 || true)"
DROPPED_COMBO="${DROPPED_COMBO:-0}"

jq -r '.summary' "$PAYLOAD_FILE" >"$SUMMARY_FILE"

post_args=(
  --owner "$BASE_OWNER"
  --repo "$BASE_REPO"
  --number "$PR_NUMBER"
  --head-sha "$HEAD_OID"
  --head-repo-url "$HEAD_REPO_URL"
  --summary-file "$SUMMARY_FILE"
  --anchored "$ANCHORED_FILE"
  --unanchored "$UNANCHORED_FILE"
  --dropped-combo "$DROPPED_COMBO"
)
if [[ $OWN_PR -eq 1 ]]; then
  post_args+=(--own-pr)
  log_step "submitting COMMENT review (own PR)"
else
  log_step "posting Pending review"
fi
if ! bash "$SCRIPT_DIR/create-review.sh" "${post_args[@]}" 2>"$POST_ERR"; then
  cat "$POST_ERR" >&2
  category="$(extract_category "$POST_ERR")"
  reason="gh api POST failed"
  if [[ "$category" == "pending-conflict" ]]; then
    reason="existing pending review on PR — submit or cancel via UI before re-running"
  fi
  log_failure "$category" "$PR_URL" "$HEAD_OID" "$reason"
  exit 1
fi

# The review landed: edit the status comment in place into its terminal state
# (#60). N counts every surfaced finding: inline (anchored) plus relocated
# (unanchored). It is a status figure, not the findings themselves, which stay
# in the Review object.
findings_total=$(($(jq 'length' "$ANCHORED_FILE") + $(jq 'length' "$UNANCHORED_FILE")))
finding_noun="findings"
[[ "$findings_total" -eq 1 ]] && finding_noun="finding"
reviewed_body="$(render_status_comment \
  "✅ Reviewed $(status_sha_link "$HEAD_REPO_URL" "$HEAD_OID"): ${findings_total} ${finding_noun}" \
  "$STATUS_SCOPE" "$STATUS_FILE_COUNT" "$STATUS_FILES")"
edit_status_comment "$BASE_OWNER" "$BASE_REPO" "$STATUS_COMMENT_ID" "$reviewed_body"

log_step "done"
