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

# Bind every log line below to this PR so parallel reviews are attributable in
# the interleaved daemon output (the plain [pr-review-agent] prefix is for
# cycle-level lines in poll.sh/run.sh).
log_set_pr_context "$BASE_REPO" "$PR_NUMBER"

# Set after `gh pr view`; leave blank so log_failure pre-view still has the
# placeholder field populated.
HEAD_OID=""

# Durable edit-in-place status comment id (#60); set once posted/reused, edited
# in place — never deleted, so it outlives the run. Not touched by cleanup().
STATUS_COMMENT_ID=""

# Flips to 1 once the review reaches a successful terminal outcome (posted or
# intentionally skipped per ADR 0020); the failure trap reads it to leave a
# landed review's status comment alone (#180).
STATUS_DONE=0

# Latest system-failure category, set by log_failure; the failure trap turns it
# into the failed status head-line's reason (#180).
LAST_FAILURE_CATEGORY=""

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

# flip_status_failed <exit-code>
# On a system failure after the status comment went live, flip it from
# "Reviewing…" to a failed head-line so a persistent failure stops reading as a
# frozen "Reviewing…" forever (#180, ADR 0005 amendment). Best-effort, like
# edit_status_comment: never changes the exit code, ADR 0005 still posts no review
# object and exits non-zero. No-ops on success, when the review already reached a
# terminal state (STATUS_DONE), or before the comment is live, so a pre-comment
# preflight failure stays silent (unchanged from ADR 0005). The next successful
# tick reuses the same comment and overwrites failed → Reviewing → Reviewed.
flip_status_failed() {
  local rc="$1"
  [[ "$rc" -ne 0 ]] || return 0
  [[ "${STATUS_DONE:-0}" -eq 0 ]] || return 0
  [[ -n "${STATUS_COMMENT_ID:-}" ]] || return 0
  local reason failed_head failed_block failed_body
  reason="$(status_failure_reason "${LAST_FAILURE_CATEGORY:-unknown}" || true)"
  failed_head="⚠️ Review failed for $(status_sha_link "$HEAD_REPO_URL" "$HEAD_OID"), will retry next cycle"
  # Reason rides the body-block slot as a blockquote, where a clean review's
  # verdict sits, so the failed comment keeps the Reviewed comment's rhythm (#180).
  failed_block=""
  [[ -n "$reason" ]] && failed_block="> ${reason}"
  failed_body="$(render_status_comment \
    "$failed_head" "$STATUS_SCOPE" "$STATUS_FILE_COUNT" "$STATUS_FILES" "$failed_block")"
  edit_status_comment "$BASE_OWNER" "$BASE_REPO" "$STATUS_COMMENT_ID" "$failed_body"
  log_info "status comment flipped to failed (${LAST_FAILURE_CATEGORY:-unknown})"
  return 0
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

# resolution: commit-driven thread resolution (#125, ADR 0017; stamp model ADR 0019).
# Finds prior open, daemon-owned Findings whose flagged line this increment touched and
# are not yet stamped, asks the fix-check agent whether each defect is gone at HEAD, and
# on a fix stamps the Finding's comment resolved in place then resolves the thread. Also
# re-resolves any thread already carrying a stamp whose earlier resolve dropped under
# rate-limit (retry, §4). Reads run-scoped globals (SCRATCH at HEAD, DIFF_FILE, OPERATOR,
# diff_scoped, HEAD_*). Best-effort: the review has already landed when it runs, so every
# failure is logged and returns 0 rather than failing the PR-tick.
resolution() {
  local threads_file candidates_file retry_file notes_file notes_jsonl finding_file judge_raw
  threads_file="$SCRATCH/.pr-review-threads.json"
  candidates_file="$SCRATCH/.pr-review-candidates.json"
  retry_file="$SCRATCH/.pr-review-retry.json"
  notes_file="$SCRATCH/.pr-review-stamps.json"
  notes_jsonl="$SCRATCH/.pr-review-stamps.jsonl"

  if ! fetch_open_review_threads "$BASE_OWNER" "$BASE_REPO" "$PR_NUMBER" >"$threads_file"; then
    log_info "reviewThreads query failed; skipping resolution"
    return 0
  fi

  # Threads already carrying a resolution stamp whose resolve dropped earlier:
  # re-resolve only, no re-judgment and no second stamp (ADR 0017 §4, ADR 0019).
  if ! python3 "$SCRIPT_DIR/resolve_threads.py" select-retry \
    --threads "$threads_file" --operator "$OPERATOR" >"$retry_file"; then
    printf '[]' >"$retry_file"
  fi

  # Candidates to judge: open daemon threads, not yet noted, whose flagged line
  # this increment touched, plus up to RESOLVE_UNTOUCHED_CAP threads whose fix may
  # have landed away from the flagged line (#172, e.g. a missing test added in a
  # new file). On a force-push/rebase the increment diff couldn't be computed
  # (diff_scoped=0), so DIFF_FILE is the full PR diff, whose old side is the base
  # branch, not the coordinate space the Findings' creation-side lines live in;
  # take every open daemon thread there (ADR 0017 §1) rather than line-filter
  # against the wrong side.
  local untouched_cap="${RESOLVE_UNTOUCHED_CAP:-5}"
  local select_args=(--threads "$threads_file" --diff "$DIFF_FILE" --operator "$OPERATOR"
    --untouched-cap "$untouched_cap")
  if [[ $diff_scoped -eq 0 ]]; then
    select_args+=(--all-open)
  fi
  if ! python3 "$SCRIPT_DIR/resolve_threads.py" select "${select_args[@]}" >"$candidates_file"; then
    log_info "candidate selection failed; skipping judgment"
    printf '[]' >"$candidates_file"
  fi

  : >"$notes_jsonl"
  local n
  n="$(jq 'length' "$candidates_file")"
  if [[ "$n" -gt 0 ]]; then
    log_info "judging ${n} candidate thread(s) for commit-driven resolution"
    # Focused single-file judgment, so a shorter backstop than the full review's 300s.
    local fix_check_timeout="${FIX_CHECK_AGENT_TIMEOUT:-180}"
    local i tid path line verdict fixed rationale rc
    for ((i = 0; i < n; i++)); do
      tid="$(jq -r ".[$i].thread_id" "$candidates_file")"
      path="$(jq -r ".[$i].path" "$candidates_file")"
      line="$(jq -r ".[$i].line" "$candidates_file")"
      finding_file="$SCRATCH/.pr-review-finding-${i}.json"
      jq ".[$i] | {path, line, finding_body}" "$candidates_file" >"$finding_file"

      judge_raw="$SCRATCH/.pr-review-judge-${i}.txt"
      rc=0
      (
        cd "$SCRATCH"
        run_with_timeout "$fix_check_timeout" \
          claude -p "/judge-fix $PR_URL --finding $(basename "$finding_file")" \
          --output-format stream-json --verbose |
          python3 "$SCRIPT_DIR/stream_format.py" --raw-out "$judge_raw"
      ) || rc=$?
      if [[ "$rc" -ne 0 || ! -s "$judge_raw" ]]; then
        log_info "fix-check failed for ${path}:${line} (${tid}), rc=${rc}; leaving open"
        continue
      fi

      verdict="$(python3 "$SCRIPT_DIR/resolve_threads.py" parse-verdict "$judge_raw")"
      fixed="$(jq -r '.fixed' <<<"$verdict")"
      rationale="$(jq -r '.rationale' <<<"$verdict")"
      if [[ "$fixed" == "true" ]]; then
        log_info "fix detected ${path}:${line} (${tid}): ${rationale}"
        # Pull comment_id (the in-place edit target) and finding_body straight from
        # the candidate, so the multi-line body never round-trips through a shell var.
        jq -c --argjson i "$i" --arg rationale "$rationale" \
          '.[$i] | {thread_id, comment_id, path, line, finding_body, rationale: $rationale}' \
          "$candidates_file" >>"$notes_jsonl"
      else
        log_info "left open ${path}:${line} (${tid}): ${rationale}"
      fi
    done
  fi
  jq -sc '.' "$notes_jsonl" >"$notes_file" 2>/dev/null || printf '[]' >"$notes_file"

  local notes_n retry_n
  notes_n="$(jq 'length' "$notes_file")"
  retry_n="$(jq 'length' "$retry_file")"
  if [[ "$notes_n" -eq 0 && "$retry_n" -eq 0 ]]; then
    log_info "nothing to resolve (no fixed findings, no retries)"
    return 0
  fi

  log_info "stamping ${notes_n} resolved finding(s), retrying ${retry_n} resolve(s)"
  # The commit-driven path edits the Finding comment in place (ADR 0019); it opens no
  # review, so no pending-wrapper to pass or pre-clean (unlike the reply path).
  python3 "$SCRIPT_DIR/resolve_threads.py" act \
    --notes "$notes_file" --retry "$retry_file" \
    --head-owner "$HEAD_REPO_OWNER" --head-repo "$HEAD_REPO_NAME" --head-sha "$HEAD_OID" ||
    log_info "resolution stamping failed (non-fatal)"
  return 0
}

meta="$(gh pr view "$PR_URL" --json id,headRepository,headRepositoryOwner,headRefName,headRefOid,author)"
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
# both handlers no-op on the still-empty globals. flip_status_failed runs first
# so the (durable) status comment is updated before cleanup tears down the
# (run-scoped) lock and scratch; it reads $? for the exit code (#180).
if ! LOCK_FILE="$(acquire_pr_lock "$BASE_OWNER" "$BASE_REPO" "$PR_NUMBER")"; then
  log_info "review already in progress for ${BASE_OWNER}/${BASE_REPO}#${PR_NUMBER}, skipping"
  exit 0
fi
trap 'flip_status_failed $?; cleanup' EXIT

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
# The agents read a line-numbered copy so they read `line` off the leading number
# instead of counting hunk lines (ADR 0018). The raw DIFF_FILE stays the
# pipeline's input (anchor_findings split, commit-driven resolution).
NUMBERED_BASENAME=".pr-review-diff-numbered.txt"
NUMBERED_FILE="$SCRATCH/$NUMBERED_BASENAME"
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
# create-review.sh echoes the full gh-api review JSON on success. Capture it here
# rather than let it leak to the operator's terminal; the review id is parsed out
# for a one-line success log.
POST_OUT="$SCRATCH/.pr-review-post.out"

log_step "fetching diff"
# When --last-sha is set, scope the diff to changes since the prior review's
# HEAD so the agent only re-reads what's new. Falls back to the full PR diff
# when last_sha wasn't passed (first-review), is no longer an ancestor of HEAD
# after a force-push/rebase (#123: an incremental diff across diverged tips reads
# the wrong delta), or can't be fetched into the shallow clone for the diff.
# The fast-forward check runs first so a force-push skips the now-pointless
# fetch; it asks GitHub's compare API, not the shallow local history (#149).
diff_scoped=0
if [[ -n "$LAST_SHA" ]]; then
  if is_fast_forward "$HEAD_REPO" "$LAST_SHA" "$HEAD_OID"; then
    if (cd "$SCRATCH" && git fetch --quiet origin "$LAST_SHA" 2>/dev/null); then
      (cd "$SCRATCH" && git diff "$LAST_SHA..HEAD") >"$DIFF_FILE"
      diff_scoped=1
      log_info "diff scoped to ${LAST_SHA:0:12}..HEAD"
    else
      log_info "could not fetch ${LAST_SHA:0:12}, falling back to full PR diff"
    fi
  else
    log_info "non-fast-forward since ${LAST_SHA:0:12} (force-push/rebase), using full PR diff"
  fi
fi
if [[ $diff_scoped -eq 0 ]]; then
  gh pr diff "$PR_URL" >"$DIFF_FILE"
fi

# Line-numbered diff for the agents (ADR 0018, layer A).
python3 "$SCRIPT_DIR/anchor_findings.py" number "$DIFF_FILE" >"$NUMBERED_FILE"

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
    claude -p "/review-pr $PR_URL --diff $NUMBERED_BASENAME" \
    --output-format stream-json --verbose |
    python3 "$SCRIPT_DIR/stream_format.py" --raw-out "$RAW_FILE"
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
      claude -p "/edit-review $PR_URL --diff $NUMBERED_BASENAME --payload $AUTHOR_BASENAME" \
      --output-format stream-json --verbose |
      python3 "$SCRIPT_DIR/stream_format.py" --raw-out "$EDIT_RAW_FILE"
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
python3 "$SCRIPT_DIR/anchor_findings.py" split \
  "$PAYLOAD_FILE" "$DIFF_FILE" \
  --anchored "$ANCHORED_FILE" \
  --unanchored "$UNANCHORED_FILE" \
  >"$ANCHOR_OUT"
DROPPED_COMBO="$(grep -m1 '^dropped_forbidden_combo=' "$ANCHOR_OUT" | cut -d= -f2 || true)"
DROPPED_COMBO="${DROPPED_COMBO:-0}"

jq -r '.summary' "$PAYLOAD_FILE" >"$SUMMARY_FILE"

# New findings this tick: inline (anchored) plus relocated (unanchored). Zero
# means the increment raised nothing new, so a posted review would be an empty
# per-SHA object stacked on the PR (ADR 0020); skip the POST and let the status
# index below carry the tick's effect (a resolution-only push still flips threads
# resolved). unanchored_count also feeds the index's "outside the diff" pointer.
anchored_count="$(jq 'length' "$ANCHORED_FILE")"
unanchored_count="$(jq 'length' "$UNANCHORED_FILE")"
new_findings_total=$((anchored_count + unanchored_count))

review_url=""
if [[ "$new_findings_total" -gt 0 ]]; then
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
  if ! bash "$SCRIPT_DIR/create-review.sh" "${post_args[@]}" >"$POST_OUT" 2>"$POST_ERR"; then
    cat "$POST_ERR" >&2
    category="$(extract_category "$POST_ERR")"
    reason="gh api POST failed"
    if [[ "$category" == "pending-conflict" ]]; then
      reason="existing pending review on PR — submit or cancel via UI before re-running"
    fi
    log_failure "$category" "$PR_URL" "$HEAD_OID" "$reason"
    exit 1
  fi
  # Report the landed review by id instead of dumping the raw JSON. A parse miss
  # (unexpected shape) degrades to no id, never an error — the review did land.
  review_id="$(jq -r '.id // empty' "$POST_OUT" 2>/dev/null || true)"
  # html_url anchors the status index's "outside the diff" pointer at this review,
  # the home of any relocated finding (ADR 0005, ADR 0020 Decision 4).
  review_url="$(jq -r '.html_url // empty' "$POST_OUT" 2>/dev/null || true)"
  if [[ $OWN_PR -eq 1 ]]; then
    log_ok "submitted COMMENT review${review_id:+ #$review_id}"
  else
    log_ok "posted Pending review${review_id:+ #$review_id}"
  fi
else
  log_info "no new findings at $HEAD_OID; skipping review object (ADR 0020)"
fi

# The review reached a successful terminal outcome (posted, or intentionally
# skipped per ADR 0020). Only best-effort cosmetics run past here, so a non-zero
# exit must not flip the status comment to "failed" (#180).
STATUS_DONE=1

# Commit-driven thread resolution (#125, ADR 0017; stamp model ADR 0019). Runs
# before the terminal status edit so the findings index below reflects the threads
# this tick just resolved. Re-review only (LAST_SHA set): a first review has no
# prior daemon threads. Best-effort; resolution returns 0 on any internal failure,
# but guard the call too so set -e never trips.
if [[ -n "$LAST_SHA" ]]; then
  log_step "commit-driven resolution"
  resolution || log_info "resolution skipped (non-fatal)"
fi

# Edit the status comment into its terminal state (#60) with the cumulative
# findings index (ADR 0020), read fresh from the PR's threads so it reflects this
# tick's posts and resolves. The headline carries scope, not a per-SHA count: the
# index's rollup replaces that count. All best-effort — a failed fetch or render
# degrades to a headline-and-scope status, never aborts the landed review.
log_step "rendering status index"
index_threads_file="$SCRATCH/.pr-review-index-threads.json"
index_block=""
if fetch_open_review_threads "$BASE_OWNER" "$BASE_REPO" "$PR_NUMBER" >"$index_threads_file"; then
  index_block="$(python3 "$SCRIPT_DIR/findings_index.py" \
    --threads "$index_threads_file" --operator "$OPERATOR" \
    --unanchored "$unanchored_count" --review-url "$review_url" \
    --summary-file "$SUMMARY_FILE" 2>/dev/null || true)"
else
  log_info "thread fetch for status index failed (non-fatal)"
fi
reviewed_body="$(render_status_comment \
  "✅ Reviewed $(status_sha_link "$HEAD_REPO_URL" "$HEAD_OID")" \
  "$STATUS_SCOPE" "$STATUS_FILE_COUNT" "$STATUS_FILES" "$index_block")"
edit_status_comment "$BASE_OWNER" "$BASE_REPO" "$STATUS_COMMENT_ID" "$reviewed_body"

log_step "done"
