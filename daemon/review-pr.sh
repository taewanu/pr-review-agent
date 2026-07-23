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
DRY_RUN=0
LAST_SHA=""
AT_SHA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-scratch)
      KEEP_SCRATCH=1
      shift
      ;;
    --at-sha)
      # Review the PR as of an earlier commit (a bug later fixed within the same
      # PR), for reproducible recall measurement by the eval harness. Overrides
      # HEAD_OID so the checkout, diff, and anchoring all target that commit, and
      # diffs base...<sha> via the compare API. Implies --dry-run: findings
      # anchored to the old commit would mis-anchor if posted to the live PR.
      #
      # Pins the code only. The PR title and body are fetched live, so a
      # description rewritten since <sha> still reaches the intent lens and the
      # editor, the two that read it. A description naming the bugs under
      # measurement is an answer key, and recall from those two is then not a
      # measurement. Warned about at run time below.
      if [[ $# -lt 2 ]]; then
        log_err "--at-sha requires a value"
        exit 1
      fi
      # A short sha reaches `git fetch` as an unknown ref and fails with a raw
      # "couldn't find remote ref"; operators paste short shas from `git log`.
      if [[ ! "$2" =~ ^[0-9a-f]{40}$ ]]; then
        log_err "--at-sha needs a full 40-character sha, got '$2'"
        exit 1
      fi
      AT_SHA="$2"
      DRY_RUN=1
      KEEP_SCRATCH=1
      shift 2
      ;;
    --dry-run)
      # Measurement / local-preview mode: run the full generation pipeline
      # (lenses → merge → editor → findings) but post nothing to the PR — no
      # status comment, no review object, no thread resolution. Implies
      # --keep-scratch so the findings payload and .cost sidecars survive for a
      # caller (the eval harness) to read. Bypasses the already-reviewed sentinel
      # skip so a re-measurement of the same HEAD still runs.
      DRY_RUN=1
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
  log_err "usage: review-pr.sh [--keep-scratch] [--dry-run] [--at-sha <sha>] [--last-sha <sha>] <pr-url>"
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

# Same per-PR colour the daemon-level lines above use (pr_prefix's palette,
# keyed identically off "repo#pr"), reused for the lens/editor activity labels
# below so a PR's colour is consistent across both logging subsystems. Only
# the escape bytes travel through --label; stream_format.py stays colour-blind
# (it just wraps whatever string it's given in brackets), so _log_color_enabled
# stays single-sourced here.
PR_COLOR_START="$(_sgr "${_LOG_PR_PALETTE[$(pr_color_index "${BASE_REPO}#${PR_NUMBER}")]}")"
PR_COLOR_RESET="$(_sgr 0)"

# Set after `gh pr view`; leave blank so log_failure pre-view still has the
# placeholder field populated.
HEAD_OID=""

# Durable edit-in-place status comment id (#60); set once posted/reused, edited
# in place — never deleted, so it outlives the run. Not touched by cleanup().
STATUS_COMMENT_ID=""

# Reviewed-SHAs trail (ADR 0021), recovered from the status comment body before
# the first edit overwrites it. Declared here, assigned later, so flip_status_failed
# can read it to preserve the trail on a failed tick rather than wiping it.
STATUS_TRAIL_PRIOR=""

# Flips to 1 once the review reaches a successful terminal outcome (posted or
# intentionally skipped per ADR 0020); the failure trap reads it to leave a
# landed review's status comment alone (#180).
STATUS_DONE=0

# Latest system-failure category, set by log_failure; the failure trap turns it
# into the failed status head-line's reason (#180).
LAST_FAILURE_CATEGORY=""

# Deadline epoch of a session-limit pause, set only on that failure (#231). The
# failed status comment reads it so the author sees when polling resumes rather
# than the generic next-cycle retry.
STATUS_PAUSE_UNTIL=""

# Per-PR lock path (#67); set once acquired, released by cleanup().
LOCK_FILE=""

# Sum every lens/editor/judge-fix cost sidecar and log the total, on success or
# failure (ADR 0023 dogfood follow-up): each was only ever visible as one line
# per claude -p call in the live log, so seeing what a review actually cost, or
# a FAILED review had already burned before it failed, meant hand-summing the
# log by eye. Must run before cleanup() removes $SCRATCH (the trap order below
# guarantees this: flip_status_failed, which calls this, runs first). `find`,
# not a `*.cost` glob: every raw file (hence every .cost sidecar) is a dotfile
# (.pr-review-raw*.txt.cost), which a bare `*` glob silently excludes without
# `dotglob`. Process substitution (not a pipe) keeps the while loop in this
# shell, so total_cost survives past the loop.
log_total_review_cost() {
  [[ -n "${SCRATCH:-}" ]] || return 0
  local total_cost=0 cost_file
  while IFS= read -r cost_file; do
    total_cost="$(awk -v a="$total_cost" -v b="$(cat "$cost_file")" 'BEGIN { printf "%.3f", a + b }')"
  done < <(find "$SCRATCH" -maxdepth 1 -name "*.cost" -type f)
  if [[ "$total_cost" != "0" ]]; then
    log_info "total review cost: \$${total_cost}"
  fi
}

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
  # A failed attempt can still have burned real claude -p cost on whichever
  # lenses/editor ran before the failure (e.g. a style-violation caught only
  # after all 5 lenses and the editor completed, sounds-abroad#192): that cost
  # was previously invisible, since the success path's own cost log line never
  # ran. Independent of the guards below, which are about the status comment.
  log_total_review_cost
  [[ "${STATUS_DONE:-0}" -eq 0 ]] || return 0
  [[ -n "${STATUS_COMMENT_ID:-}" ]] || return 0
  local reason failed_head failed_block failed_body
  reason="$(status_failure_reason "${LAST_FAILURE_CATEGORY:-unknown}" || true)"
  # "next cycle" is a promise the daemon cannot keep while polling is paused, so
  # a session-limit failure names the resume time instead (#231).
  if [[ -n "${STATUS_PAUSE_UNTIL:-}" ]]; then
    failed_head="⚠️ Review paused for $(status_sha_link "$HEAD_REPO_URL" "$HEAD_OID"), retrying after $(format_clock_time "$STATUS_PAUSE_UNTIL")"
  else
    failed_head="⚠️ Review failed for $(status_sha_link "$HEAD_REPO_URL" "$HEAD_OID"), will retry next cycle"
  fi
  # Reason rides the body-block slot as a blockquote, where a clean review's
  # verdict sits, so the failed comment keeps the Reviewed comment's rhythm (#180).
  failed_block=""
  [[ -n "$reason" ]] && failed_block="> ${reason}"
  # Carry the prior trail through (ADR 0021) so a failed tick preserves the
  # reviewed-SHAs record instead of overwriting it away; the next success reads it
  # back. This tick's SHA is not added — it was not successfully reviewed.
  failed_body="$(render_status_comment \
    "$failed_head" "$STATUS_SCOPE" "$STATUS_FILE_COUNT" "$STATUS_FILES" "$failed_block" "$STATUS_TRAIL_PRIOR")"
  edit_status_comment "$BASE_OWNER" "$BASE_REPO" "$STATUS_COMMENT_ID" "$failed_body"
  log_info "status comment flipped to failed (${LAST_FAILURE_CATEGORY:-unknown})"
  return 0
}

# Recover the `category=<slug>` first stderr line the Python pipeline stages emit
# on failure (the wire contract documented at lib.sh's failure-category home).
# Falls back to the shared FAIL_UNKNOWN so the structured failure line is always
# populated.
extract_category() {
  local stderr_path="$1"
  local cat
  cat="$(grep -m1 '^category=' "$stderr_path" 2>/dev/null | cut -d= -f2 || true)"
  [[ -n "$cat" ]] && printf '%s' "$cat" || printf '%s' "$FAIL_UNKNOWN"
}

# Parse the `session_limit_deadline=<epoch>` second stderr line merge_findings.py
# emits alongside a session-limit category (#231). Empty means the sentinel was
# there but its reset time was unreadable, which the pause treats as its fixed
# fallback.
extract_session_limit_deadline() {
  local stderr_path="$1"
  grep -m1 '^session_limit_deadline=' "$stderr_path" 2>/dev/null | cut -d= -f2- || true
}

# Parse the `truncated_count=<n>` line merge_findings.py emits on a successful
# (non-error) run whose post-cap truncation dropped findings (ADR 0023). Falls
# back to 0 so an absent line (no truncation happened) is indistinguishable
# from an explicit zero, both meaning "nothing to append to the summary."
extract_truncated_count() {
  local stderr_path="$1"
  local n
  n="$(grep -m1 '^truncated_count=' "$stderr_path" 2>/dev/null | cut -d= -f2 || true)"
  [[ -n "$n" ]] && printf '%s' "$n" || printf '0'
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
  # Both caps bound the serial judge-fix loop below (#197): each candidate is
  # one claude -p call, so candidate count × FIX_CHECK_AGENT_TIMEOUT is this
  # leg's worst-case share of PER_PR_TIMEOUT.
  local untouched_cap="${RESOLVE_UNTOUCHED_CAP:-5}"
  local touched_cap="${RESOLVE_TOUCHED_CAP:-10}"
  local select_args=(--threads "$threads_file" --diff "$DIFF_FILE" --operator "$OPERATOR"
    --untouched-cap "$untouched_cap" --touched-cap "$touched_cap")
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
        # Same shared claude_slot pool as the lenses and editor (ADR 0023
        # revision): without a slot this call ran outside the pool, so with
        # MAX_PARALLEL > 1 the concurrent claude count exceeded it (#197).
        slot="$(acquire_claude_slot judge-fix)"
        cd "$SCRATCH"
        inner_rc=0
        # claude's own stderr (workspace-trust notice, etc.) goes to a sidecar
        # file rather than the primary log; see the lens loop above.
        run_with_timeout "$fix_check_timeout" \
          claude -p "/judge-fix $PR_URL --finding $(basename "$finding_file")" \
          --model "$REVIEW_MODEL" \
          --output-format stream-json --verbose \
          2>"$judge_raw.stderr" |
          python3 "$SCRIPT_DIR/stream_format.py" --raw-out "$judge_raw" \
            --label "${PR_COLOR_START}pr${PR_NUMBER}:judge-fix${PR_COLOR_RESET}" \
            --cost-out "$judge_raw.cost" ||
          inner_rc=$?
        release_claude_slot "$slot"
        exit "$inner_rc"
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
          '.[$i] | {thread_id, comment_id, path, line, head_line, head_start_line, finding_body, rationale: $rationale}' \
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

meta="$(gh pr view "$PR_URL" --json id,headRepository,headRepositoryOwner,headRefName,headRefOid,baseRefName,author,title,body,closingIssuesReferences)"
HEAD_REPO_OWNER="$(jq -r '.headRepositoryOwner.login // empty' <<<"$meta")"
HEAD_REPO_NAME="$(jq -r '.headRepository.name // empty' <<<"$meta")"
HEAD_REF="$(jq -r '.headRefName // empty' <<<"$meta")"
HEAD_OID="$(jq -r '.headRefOid // empty' <<<"$meta")"
BASE_REF="$(jq -r '.baseRefName // empty' <<<"$meta")"
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

# --at-sha pins the review to an earlier PR commit (recall measurement). Override
# HEAD_OID here, after the live head is logged, so the checkout, diff, and finding
# anchoring below all target the pinned commit. HEAD_REF stays the live head ref,
# used only for the PR-head fetch (the pinned commit is fetched by SHA).
if [[ -n "$AT_SHA" ]]; then
  HEAD_OID="$AT_SHA"
  log_info "pinned to ${AT_SHA:0:12} (--at-sha, dry-run)"
  # Stated every run, not gated on whether the body actually changed: proving it
  # did would need the description as of <sha>, which the API does not keep.
  log_info "pin covers the code only: the PR description is read live, not as of ${AT_SHA:0:12}; if it was rewritten since, the intent lens and the editor read the newer text"
fi

# Own-vs-others gates the submit path (ADR 0008): own PRs auto-submit a COMMENT
# review, others' stay pending. The operator is the gh-authenticated identity
# (ADR 0003), as in reply-pr.sh. Derived here, not passed from poll.sh, so the
# manual one-shot is correct too; a blank author falls through to the others' path.
OPERATOR="$(gh api user --jq '.login' 2>/dev/null || true)"
OWN_PR=0
if [[ -n "$PR_AUTHOR" && "$PR_AUTHOR" == "$OPERATOR" ]]; then
  OWN_PR=1
  # The submit half of this line is a claim about what happens later, so it has
  # to respect --dry-run. Stated unconditionally, a run that posts nothing read
  # as one that had just submitted a review to the operator's own PR.
  if [[ $DRY_RUN -eq 1 ]]; then
    log_info "own PR (author == operator '$OPERATOR'): dry-run, submitting nothing"
  else
    log_info "own PR (author == operator '$OPERATOR'): auto-submitting a COMMENT review"
  fi
fi

# A pause written by whichever PR tripped the quota this cycle (#231). poll.sh
# gates the next cycle, but the PRs already dispatched in this one would each
# clone, run every lens into the same wall, and flip their own status comment.
# Checked before the status comment goes live so a paused PR churns nothing;
# non-zero, so the state file does not advance and the PR is genuinely re-reviewed
# after the reset. A review already past this point still finishes into the wall:
# stopping it would mean killing live subprocesses, which buys one comment flip.
if PAUSE_DEADLINE="$(session_pause_active)"; then
  log_info "session limit pause active until $(format_clock_time "$PAUSE_DEADLINE"), skipping review"
  exit 1
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
if [[ $DRY_RUN -eq 0 ]] &&
  existing_sha="$(discover_sentinel_sha "$BASE_OWNER" "$BASE_REPO" "$PR_NUMBER" "$OPERATOR")" &&
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
  # --at-sha checks out an earlier PR commit the depth-1 head fetch above doesn't
  # include; fetch that commit by SHA (GitHub serves reachable SHAs).
  [[ -n "$AT_SHA" ]] && git fetch --quiet --depth=1 origin "$AT_SHA"
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
# ADR 0023: parallel independent lenses, each an unaware-of-the-others
# generator. Their raw outputs are unioned and deduped before the confidence gate
# (daemon/merge_findings.py), so a bug one lens misses another can still catch.
# Parallel arrays, not an associative array, so this stays bash-3.2-safe (ADR
# 0013's runtime constraint: stock macOS bash has no bash-4 associative arrays).
LENS_COMMANDS=(/review-pr /review-pr-correctness /review-pr-perf /review-pr-security /review-pr-tests /review-pr-intent)
LENS_LABELS=(default correctness perf security tests intent)
# Each path's own filename suffix names its lens; a named variable per lens
# would only be read once, right here, so the array is written inline.
LENS_RAW_FILES=(
  "$RAW_FILE"
  "$SCRATCH/.pr-review-raw-correctness.txt"
  "$SCRATCH/.pr-review-raw-perf.txt"
  "$SCRATCH/.pr-review-raw-security.txt"
  "$SCRATCH/.pr-review-raw-tests.txt"
  "$SCRATCH/.pr-review-raw-intent.txt"
)
# ADR 0035: the intent lens reads the change's stated intent alongside the diff,
# so unlike the code lenses it needs an input file. Bare name like the diff,
# since the lens reads it from the scratch cwd.
INTENT_BASENAME=".pr-review-intent.md"
INTENT_FILE="$SCRATCH/$INTENT_BASENAME"
# ADR 0034: restrict the active lens set via REVIEW_LENSES, a space-separated list
# of labels (e.g. "default" or "default correctness"). Collapsing to fewer lenses
# is the one lever that cuts review cost proportionally, trading the recall that
# independent reads buy (ADR 0022/0023): measured, one lens misses the hard
# co-varying-state class the full set catches.
# Filtering the three parallel arrays together preserves their index alignment;
# the merge and gate are already lens-count-agnostic (ADR 0023 Decision 3).
REVIEW_LENSES="$(resolve_tunable REVIEW_LENSES "$SCRIPT_DIR/../.env")"
# Default set when unset (#249): the code sweep, its correctness deepening, and
# intent. The three domain lenses (perf, security, tests) are off by default,
# because nothing in this repo's review history has been a perf, security, or
# test-quality finding, so the burden of proof is on keeping a domain lens, not on
# removing it (ADR 0035). A fork where that domain is the point re-enables it here
# or in .env. The full six stay selectable; this only changes the unset default.
REVIEW_LENSES="${REVIEW_LENSES:-default correctness intent}"
if [[ -n "${REVIEW_LENSES:-}" ]]; then
  _sel_cmds=()
  _sel_labels=()
  _sel_raws=()
  for _i in "${!LENS_LABELS[@]}"; do
    for _want in $REVIEW_LENSES; do
      if [[ "${LENS_LABELS[$_i]}" == "$_want" ]]; then
        _sel_cmds+=("${LENS_COMMANDS[$_i]}")
        _sel_labels+=("${LENS_LABELS[$_i]}")
        _sel_raws+=("${LENS_RAW_FILES[$_i]}")
        break
      fi
    done
  done
  if [[ ${#_sel_labels[@]} -eq 0 ]]; then
    log_err "REVIEW_LENSES='$REVIEW_LENSES' matched no known lens (default correctness perf security tests intent)"
    exit 1
  fi
  LENS_COMMANDS=("${_sel_cmds[@]}")
  LENS_LABELS=("${_sel_labels[@]}")
  LENS_RAW_FILES=("${_sel_raws[@]}")
  log_info "REVIEW_LENSES active: ${LENS_LABELS[*]}"
fi

# ADR 0035: assemble what the change says it does, for the intent lens to read
# against the diff. Two rungs. The PR's own title and body come free with the
# metadata fetch above; the body behind each closing reference costs a call per
# issue and is only there when the author wrote one. A rung that fails is named
# in the file rather than left blank, so the lens reads a gap as less evidence
# instead of as license to guess.
build_intent_file() {
  local n owner repo issue_rc
  {
    printf '# What this change says it does\n\n'
    printf '## PR title\n\n%s\n\n' "$(jq -r '.title // ""' <<<"$meta")"
    printf '## PR description\n\n'
    jq -r 'if (.body // "") == "" then "(empty)" else .body end' <<<"$meta"
    printf '\n'
  } >"$INTENT_FILE"

  while read -r n owner repo; do
    [[ -z "$n" ]] && continue
    issue_rc=0
    # Each closing reference carries its own repo, so a cross-repo `Closes` is
    # fetched from where the issue actually lives, not from the PR's base.
    run_with_timeout "${GH_API_CALL_TIMEOUT:-90}" \
      gh issue view "$n" --repo "$owner/$repo" --json title,body \
      >"$SCRATCH/.pr-review-issue-$n.json" 2>/dev/null || issue_rc=$?
    if [[ "$issue_rc" -ne 0 ]]; then
      printf '\n## Linked issue %s/%s#%s\n\n(unreadable: private, deleted, or the fetch failed)\n' \
        "$owner" "$repo" "$n" >>"$INTENT_FILE"
      log_info "intent: ${owner}/${repo}#${n} unreadable, continuing without it"
      continue
    fi
    {
      printf '\n## Linked issue %s/%s#%s: ' "$owner" "$repo" "$n"
      jq -r '.title // ""' "$SCRATCH/.pr-review-issue-$n.json"
      printf '\n'
      jq -r 'if (.body // "") == "" then "(empty)" else .body end' \
        "$SCRATCH/.pr-review-issue-$n.json"
      printf '\n'
    } >>"$INTENT_FILE"
  done < <(jq -r '.closingIssuesReferences[]? |
    "\(.number) \(.repository.owner.login) \(.repository.name)"' <<<"$meta")
}

# The lens is skipped when the change states nothing to contradict. Title alone
# does not count: it is too short to contradict a diff, and counting it would run
# the lens on every PR, which is the cost this branch exists to avoid. HTML
# comments are stripped first so a PR template's boilerplate does not read as a
# description the author wrote.
intent_active=0
for _label in "${LENS_LABELS[@]}"; do
  if [[ "$_label" == "intent" ]]; then
    intent_active=1
  fi
done
intent_body_len="$(jq -r '.body // ""' <<<"$meta" |
  python3 -c 'import re,sys; print(len(re.sub(r"<!--.*?-->", "", sys.stdin.read(), flags=re.S).strip()))')"
intent_issue_count="$(jq '.closingIssuesReferences | length' <<<"$meta")"
# The skip needs another lens to fall back to. Under REVIEW_LENSES=intent it
# would otherwise empty the lens set, leaving the merge with no payload at all,
# so an intent-only config runs the lens and lets it return no findings.
intent_only=0
if [[ "${#LENS_LABELS[@]}" -eq 1 && "$intent_active" -eq 1 ]]; then
  intent_only=1
fi
if [[ "$intent_active" -eq 0 ]]; then
  # REVIEW_LENSES excludes the lens, so nothing will read the file. Building it
  # anyway costs a `gh issue view` per closing reference every polling cycle, and
  # a per-PR network call is hang surface whether or not its result is used.
  :
elif [[ "$intent_body_len" -gt 0 || "$intent_issue_count" -gt 0 || "$intent_only" -eq 1 ]]; then
  build_intent_file
else
  _kept_cmds=()
  _kept_labels=()
  _kept_raws=()
  for _i in "${!LENS_LABELS[@]}"; do
    if [[ "${LENS_LABELS[$_i]}" != "intent" ]]; then
      _kept_cmds+=("${LENS_COMMANDS[$_i]}")
      _kept_labels+=("${LENS_LABELS[$_i]}")
      _kept_raws+=("${LENS_RAW_FILES[$_i]}")
    fi
  done
  LENS_COMMANDS=("${_kept_cmds[@]}")
  LENS_LABELS=("${_kept_labels[@]}")
  LENS_RAW_FILES=("${_kept_raws[@]}")
  log_info "intent lens skipped: no description and no linked issue"
fi

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
if [[ -n "$AT_SHA" ]]; then
  # Pinned-commit review (--at-sha): diff base...<sha> via the compare API, the
  # at-sha analog of `gh pr diff`. Server-side merge-base diff, so the shallow
  # clone's missing local merge-base is a non-issue. Same-repo PRs only (the base
  # lives in BASE_OWNER/BASE_REPO); a fork's base would need a cross-repo compare.
  GH_API_CALL_TIMEOUT="${GH_API_CALL_TIMEOUT:-90}"
  at_diff_rc=0
  run_with_timeout "$GH_API_CALL_TIMEOUT" \
    gh api "repos/${BASE_OWNER}/${BASE_REPO}/compare/${BASE_REF}...${AT_SHA}" \
    -H "Accept: application/vnd.github.diff" >"$DIFF_FILE" || at_diff_rc=$?
  if [[ "$at_diff_rc" -ne 0 ]]; then
    log_failure "$FAIL_DIFF_FETCH_FAILED" "$PR_URL" "$HEAD_OID" \
      "compare ${BASE_REF}...${AT_SHA:0:12} exited $at_diff_rc"
    exit 1
  fi
elif [[ -n "$LAST_SHA" ]]; then
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
if [[ -z "$AT_SHA" && $diff_scoped -eq 0 ]]; then
  # Explicit timeout (review fix, ADR 0023): PER_PR_TIMEOUT's default grew to
  # cover the 5-lens review-agent leg's worst case, which widened the window
  # before an unrelated, unbounded gh call (this one) gets caught if it hangs.
  # A dedicated small bound keeps a stalled `gh pr diff` fast to detect
  # regardless of how large the outer per-PR watchdog needs to be.
  GH_API_CALL_TIMEOUT="${GH_API_CALL_TIMEOUT:-90}"
  diff_rc=0
  run_with_timeout "$GH_API_CALL_TIMEOUT" gh pr diff "$PR_URL" >"$DIFF_FILE" || diff_rc=$?
  if [[ "$diff_rc" -eq "$TIMEOUT_EXIT" ]]; then
    log_failure "$FAIL_DIFF_FETCH_TIMEOUT" "$PR_URL" "$HEAD_OID" "gh pr diff exceeded ${GH_API_CALL_TIMEOUT}s"
    exit 1
  elif [[ "$diff_rc" -ne 0 ]]; then
    log_failure "$FAIL_DIFF_FETCH_FAILED" "$PR_URL" "$HEAD_OID" "gh pr diff exited $diff_rc"
    exit 1
  fi
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
# Named unconditionally, not just when it differs from the repo's default
# branch: knowing the default would need its own `gh repo view` call, and a
# stacked PR (this PR's base is another open PR's branch, not main) is common
# enough in a solo/incremental workflow that the operator should not have to
# guess from context which diff they're looking at.
[[ -n "$BASE_REF" ]] && STATUS_SCOPE="${STATUS_SCOPE} (base: \`${BASE_REF}\`)"
# Find the status comment first, then recover the reviewed-SHAs trail (ADR 0021)
# from its current body before the edits below overwrite it. The trail is the one
# part of the comment that accumulates across ticks rather than being derived from
# current state, so its prior rows live nowhere but the comment body itself. A
# fetch or parse miss degrades to a restarted trail (best-effort), never aborts.
# STATUS_TRAIL_PRIOR is read by flip_status_failed too, so a failed tick preserves
# the trail rather than wiping it.
# Dry-run posts nothing, so it skips the status comment entirely. STATUS_COMMENT_ID
# stays empty, which self-disables the terminal status edit and flip_status_failed
# below (both no-op on an empty id), so no other guard is needed for them.
if [[ $DRY_RUN -eq 0 ]]; then
  STATUS_COMMENT_ID="$(find_status_comment "$BASE_OWNER" "$BASE_REPO" "$PR_NUMBER" "$OPERATOR")"
  if [[ -n "$STATUS_COMMENT_ID" ]]; then
    STATUS_TRAIL_PRIOR="$(status_comment_body "$BASE_OWNER" "$BASE_REPO" "$STATUS_COMMENT_ID" |
      python3 "$SCRIPT_DIR/status_trail.py" 2>/dev/null || true)"
  fi

  # The "Reviewing…" render carries the prior trail unchanged — this tick's SHA is
  # not reviewed yet; the terminal render below folds it in.
  reviewing_body="$(render_status_comment \
    "👀 Reviewing $(status_sha_link "$HEAD_REPO_URL" "$HEAD_OID")…" \
    "$STATUS_SCOPE" "$STATUS_FILE_COUNT" "$STATUS_FILES" "" "$STATUS_TRAIL_PRIOR")"
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
fi

# Wall-clock backstop (#76), symmetric with the reply agent. Raised from 300s
# to 600s after dogfooding the multi-lens design (ADR 0023): even in isolation
# several lenses on a real PR took 178-262s, some genuinely exceeded 300s (not
# stuck, just slow generate-verify-score work), and concurrent slot contention
# can push this further. Partial output on timeout is discarded, not parsed.
REVIEW_AGENT_TIMEOUT="${REVIEW_AGENT_TIMEOUT:-600}"

# Global claude_slot pool size (ADR 0023 revision): resolve from env, then
# .env, same pattern as CONFIDENCE_THRESHOLD below. acquire_claude_slot (in
# lib.sh, which every background lens subshell also sources) reads
# CLAUDE_SLOT_POOL_SIZE as `${CLAUDE_SLOT_POOL_SIZE:-3}`, which only sees a
# value set in .env if this shell exports it first (the daemon never sources
# .env wholesale).
CLAUDE_SLOT_POOL_SIZE="$(resolve_tunable CLAUDE_SLOT_POOL_SIZE "$SCRIPT_DIR/../.env")"
[[ -n "$CLAUDE_SLOT_POOL_SIZE" ]] && export CLAUDE_SLOT_POOL_SIZE

# Review reasoning model (#209). Every claude -p below pins this instead of
# inheriting the operator's global ~/.claude/settings.json model, which silently
# reviewed on whatever their shell happened to prefer. Resolved once here so the
# lenses, editor, and judge-fix can't drift onto different models mid-review.
REVIEW_MODEL="$(resolve_review_model "$SCRIPT_DIR/../.env")"
# Named in the log because an unpinned model is invisible from the outside: a
# review on the wrong one still reads like a review, which is how the original
# defect survived weeks of dogfood. The dial is only trustworthy if it says so.
log_info "model: ${REVIEW_MODEL}"

# ADR 0023 (revised): independent lenses read the same diff, each unaware of the
# others' output, so a bug one misses another can still catch. The intent lens
# (ADR 0035) is the one that reads more than the diff.
# Dispatched in parallel, each bounded by the global claude_slot pool
# (daemon/lib.sh's acquire_claude_slot/release_claude_slot), not by ADR 0013's
# MAX_PARALLEL: that dial now only bounds concurrent review-pr.sh *processes*,
# while the slot pool bounds concurrent `claude -p` *calls* directly, shared
# automatically across however many review-pr.sh processes are running (the
# slot files live on disk, not in one process's memory). A stuck lens is still
# bounded by its own REVIEW_AGENT_TIMEOUT; the slot is released in the same
# subshell that acquired it, so a killed lens's slot is freed immediately
# rather than waiting for stale-reclaim.
# ADR 0034: REVIEW_MODE picks how each lens runs, orthogonally to REVIEW_LENSES
# picking how many. `subagent` dispatches a subagent per lens (Anthropic's
# orchestrator-worker shape); `single-agent` runs the lens in the process that
# already isolates it, dropping the subagent layer that only forwarded its
# stdout. The two dials are independent: `single-agent` with REVIEW_LENSES unset
# is six single-agent lenses, not one.
#
# The mode deliberately touches nothing else. An earlier draft also lowered
# CONFIDENCE_THRESHOLD here, which silently overrode the operator's own .env
# value and made two modes incomparable in measurement: the cheaper mode was
# scored behind a looser gate than the one it was being compared against.
REVIEW_MODE="$(resolve_tunable REVIEW_MODE "$SCRIPT_DIR/../.env")"
case "${REVIEW_MODE:-subagent}" in
  single-agent)
    SINGLE_AGENT_REVIEW=1
    ;;
  subagent | "")
    SINGLE_AGENT_REVIEW=0
    ;;
  *)
    log_err "REVIEW_MODE='$REVIEW_MODE' is not a known mode (subagent single-agent)"
    exit 1
    ;;
esac
export SINGLE_AGENT_REVIEW
[[ -n "${REVIEW_MODE:-}" ]] && log_info "review mode: ${REVIEW_MODE}"

lens_i=0
lens_count="${#LENS_COMMANDS[@]}"
lens_pids=()
while [[ "$lens_i" -lt "$lens_count" ]]; do
  lens_cmd="${LENS_COMMANDS[$lens_i]}"
  lens_label="${LENS_LABELS[$lens_i]}"
  lens_raw="${LENS_RAW_FILES[$lens_i]}"
  log_step "running review agent ($lens_label lens) via claude -p"
  (
    slot="$(acquire_claude_slot "$lens_label lens")"
    cd "$SCRATCH"
    rc=0
    # `claude`'s own stderr (e.g. its non-interactive workspace-trust notice,
    # expected every run since a fresh scratch clone is never pre-trusted) was
    # bypassing stream_format.py's labeling and flooding the daemon log
    # unlabeled; it goes to a per-lens sidecar file instead, so nothing is
    # silently lost but the primary log stays legible.
    if [[ "${SINGLE_AGENT_REVIEW:-0}" -eq 1 ]]; then
      # ADR 0034: run the lens in this process instead of through a subagent. The
      # slash command a subagent-mode lens invokes does nothing but dispatch one
      # subagent and forward its stdout, so the parent's harness load buys no
      # isolation the separate process does not already give. The agent's own body
      # (yaml frontmatter stripped) is appended to the base prompt, keeping the
      # investigation scaffolding the verify step needs, and slash commands are
      # disabled because the prompt below IS the instruction: a dispatch command
      # would re-enter the subagent path this mode exists to skip.
      # One file, no includes: an agent's body is its whole system prompt, so a
      # pointer at another agent's prompt resolves to nothing at runtime.
      single_sys="$SCRATCH/.pr-review-single-sys-$lens_label.md"
      awk 'BEGIN { n = 0 } /^---$/ { n++; next } n >= 2 { print }' \
        ".claude/agents/review-agent-$lens_label.md" >"$single_sys"
      # The diff goes by path, not inlined: inlining it and saying "reason over
      # this" made the model treat the context as complete and skip opening the
      # callers, which is the verify step's whole substance (ADR 0022).
      single_prompt="$SCRATCH/.pr-review-single-prompt-$lens_label.txt"
      {
        printf 'Review this PR per your instructions and emit the JSON payload.\n'
        printf 'The line-numbered diff is at: %s\n' "$NUMBERED_BASENAME"
        # ADR 0035: the intent lens gets the second side of its comparison. Every
        # other lens has only the diff, which is the whole reason it exists.
        if [[ "$lens_label" == "intent" ]]; then
          printf 'What the change says it does is at: %s\n' "$INTENT_BASENAME"
        fi
        printf 'Read it, then investigate the surrounding code with your tools '
        printf '(open the callers, trace the data flow) to verify each candidate before scoring.\n'
      } >"$single_prompt"
      run_with_timeout "$REVIEW_AGENT_TIMEOUT" \
        claude -p --append-system-prompt-file "$single_sys" \
        --model "$REVIEW_MODEL" \
        --tools Read Grep Glob Bash --strict-mcp-config --setting-sources project \
        --disable-slash-commands \
        --output-format stream-json --verbose <"$single_prompt" \
        2>"$lens_raw.stderr" |
        python3 "$SCRIPT_DIR/stream_format.py" --raw-out "$lens_raw" \
          --label "${PR_COLOR_START}pr${PR_NUMBER}:${lens_label}${PR_COLOR_RESET}" \
          --cost-out "$lens_raw.cost" ||
        rc=$?
    else
      lens_args="$lens_cmd $PR_URL --diff $NUMBERED_BASENAME"
      # ADR 0035, as above: only the intent lens takes a second input.
      if [[ "$lens_label" == "intent" ]]; then
        lens_args="$lens_args --intent $INTENT_BASENAME"
      fi
      run_with_timeout "$REVIEW_AGENT_TIMEOUT" \
        claude -p "$lens_args" \
        --model "$REVIEW_MODEL" \
        --output-format stream-json --verbose \
        2>"$lens_raw.stderr" |
        python3 "$SCRIPT_DIR/stream_format.py" --raw-out "$lens_raw" \
          --label "${PR_COLOR_START}pr${PR_NUMBER}:${lens_label}${PR_COLOR_RESET}" \
          --cost-out "$lens_raw.cost" ||
        rc=$?
    fi
    release_claude_slot "$slot"
    exit "$rc"
  ) &
  lens_pids[lens_i]=$!
  lens_i=$((lens_i + 1))
done

# Wait for every backgrounded lens, in dispatch order, checking each one's
# outcome. wait "$pid" returns that PID's own exit status, unaffected by which
# other lenses finish first. Every PID is waited on unconditionally, even when
# an earlier one timed out or produced nothing: a self-review (pr-review-agent
# PR #192) caught the prior version exiting immediately on the first bad lens,
# which left later-dispatched, still-running lenses (a 5-lens/3-slot pool means
# the last dispatched lenses start their own timeout clock later in wall time,
# so this is the normal case, not an edge case) as orphaned background jobs
# racing the EXIT trap's `cleanup()`, which `rm -rf`s $SCRATCH out from under
# whatever they were still writing to. A timed-out or empty lens is logged and
# skipped, not fatal to the whole review: merge_findings.py already tolerates
# a lens payload that fails to parse (ADR 0024), including an empty one, so the
# other lenses' valid findings still reach the confidence gate. Lives in
# lib.sh, not inline, so test_lens_wait.py can source it and assert this
# (ADR 0026, #192 follow-up).
wait_for_lens_pids

# Confidence gate threshold (ADR 0022): resolve from env, then .env, and export
# so merge_findings.py's os.environ read sees it. The daemon never sources .env
# wholesale, so without this the operator's .env dial would silently no-op and
# the gate would sit at the Python default (80). An unset value stays unexported
# so Python keeps its default.
CONFIDENCE_THRESHOLD="$(resolve_tunable CONFIDENCE_THRESHOLD "$SCRIPT_DIR/../.env")"
[[ -n "$CONFIDENCE_THRESHOLD" ]] && export CONFIDENCE_THRESHOLD

# Findings cap (#199): same resolve-and-export shape as the gate above. This
# replaces the .pr-review.yaml max_findings key, which was parsed but never
# read while the enforced cap sat hard-coded in extract_json.py.
MAX_FINDINGS="$(resolve_tunable MAX_FINDINGS "$SCRIPT_DIR/../.env")"
[[ -n "$MAX_FINDINGS" ]] && export MAX_FINDINGS

log_step "merging lens payloads"
# --no-style: the voice gate moved behind the editor (ADR 0016). This union
# only schema-validates each lens's draft, dedupes, and shapes the result to
# hand to the editor; the final gate runs in apply_edits.py, on what is posted.
if ! python3 "$SCRIPT_DIR/merge_findings.py" --no-style "${LENS_RAW_FILES[@]}" \
  >"$AUTHOR_FILE" 2>"$EXTRACT_ERR"; then
  cat "$EXTRACT_ERR" >&2
  MERGE_CATEGORY="$(extract_category "$EXTRACT_ERR")"
  if [[ "$MERGE_CATEGORY" == "session-limit" ]]; then
    # Every lens hit the subscription quota, so every PR this cycle and the next
    # would hit the same wall; pause polling until the reset instead (#231).
    PAUSE_UNTIL="$(session_pause_write "$(extract_session_limit_deadline "$EXTRACT_ERR")")"
    log_info "session limit hit, pausing polling until $(format_clock_time "$PAUSE_UNTIL")"
    # Carried into the failed status comment so the author reads the resume time,
    # not the head-line's default promise of a retry next cycle.
    STATUS_PAUSE_UNTIL="$PAUSE_UNTIL"
  fi
  log_failure "$MERGE_CATEGORY" "$PR_URL" "$HEAD_OID" "merge_findings.py exited non-zero"
  exit 1
fi
# The merge succeeded, but its stderr may still carry quality-degradation
# warnings (a lens skipped, findings dropped); forward them before cleanup
# deletes the scratch dir, or a broken lens stays invisible forever (#196).
log_degradation_warnings "$EXTRACT_ERR"
# Surfaced in the posted summary (ADR 0023), not just this stderr log: a
# healthy multi-lens union clearing the cap is not an error, so the count
# still needs a visible trace an operator can actually see.
TRUNCATED_COUNT="$(extract_truncated_count "$EXTRACT_ERR")"

# Editorial pass (#133, ADR 0016): a fresh editor agent re-reads the PR at HEAD
# and refines the draft (drop weak findings, sharpen survivors, reconcile the
# summary) before posting. Skipped on a zero-finding draft, where there is
# nothing to refine; apply_edits.py still runs the moved voice gate either way.
# SKIP_EDITOR=1 also bypasses it (#209 token measurement): apply_edits then posts
# the merged, gated draft directly, testing whether the editor's precision cleanup
# earns its separate claude -p or the gated lens output already suffices.
# --on-editor-error post-author: an unusable editor result (a miscount, corrupted
# body, or unparseable output) posts the merged author draft with a bypass note
# instead of discarding a review that already passed generation (#258). The
# author draft is independently valid and gets its own gate; apply_edits keeps
# --author on the clean file, so the fallback never applies a partial edit.
EDIT_ARGS=(--author "$AUTHOR_FILE" --truncated-count "$TRUNCATED_COUNT" --on-editor-error post-author)
if [[ "${SKIP_EDITOR:-0}" -ne 1 && "$(jq '.comments | length' "$AUTHOR_FILE")" -gt 0 ]]; then
  # Stamp an explicit index onto each finding so the editor reads it rather than
  # counting array positions, a fragile cue it miscounted (#258). apply_edits
  # still reads the clean $AUTHOR_FILE, so the extra key never reaches a post.
  AUTHOR_INDEXED_BASENAME=".pr-review-author-indexed.json"
  AUTHOR_INDEXED_FILE="$SCRATCH/$AUTHOR_INDEXED_BASENAME"
  python3 "$SCRIPT_DIR/index_findings.py" <"$AUTHOR_FILE" >"$AUTHOR_INDEXED_FILE"
  log_step "running editor agent via claude -p"
  # Same unbounded `claude -p` shape and backstop as the review agent, raised
  # to 600s for the same dogfood-observed reason. Not backgrounded (nothing to
  # fan out to; it's the one call after the lenses), but still draws from the
  # shared claude_slot pool (ADR 0023 revision), since other PRs' lenses/
  # editors may be running concurrently.
  EDITOR_AGENT_TIMEOUT="${EDITOR_AGENT_TIMEOUT:-600}"
  edit_rc=0
  (
    slot="$(acquire_claude_slot editor)"
    cd "$SCRATCH"
    rc=0
    # See the lens loop above: claude's own stderr goes to a sidecar file, not
    # the primary log.
    if [[ "${SINGLE_AGENT_REVIEW:-0}" -eq 1 ]]; then
      # The editor runs in the same shape as the lenses under this mode, and for
      # the same reason: its slash command only dispatches the editor subagent.
      # It keeps its tools because ADR 0016's verify-at-HEAD step needs them, and
      # because a tools-free editor could not reliably emit its decisions JSON on
      # a complex draft.
      editor_sys="$SCRATCH/.pr-review-single-sys-editor.md"
      awk 'BEGIN { n = 0 } /^---$/ { n++; next } n >= 2 { print }' \
        ".claude/agents/review-agent-editor.md" >"$editor_sys"
      editor_prompt="$SCRATCH/.pr-review-single-editor-prompt.txt"
      {
        printf 'Edit this draft review and emit your decisions JSON per your instructions.\n\n'
        printf '=== DRAFT PAYLOAD (findings to keep/drop/rewrite) ===\n'
        cat "$AUTHOR_INDEXED_BASENAME"
        printf '\n\n=== DIFF (line-numbered) ===\n'
        cat "$NUMBERED_BASENAME"
        # ADR 0035: an intent finding is verified against the change's stated
        # intent, not against the code, so the editor needs that side too. The
        # file exists only when the lens ran, and only that lens emits a finding
        # needing it, so an empty test covers both.
        if [[ -s "$INTENT_FILE" ]]; then
          printf '\n\n=== WHAT THE CHANGE SAYS IT DOES (for intent findings) ===\n'
          cat "$INTENT_BASENAME"
        fi
      } >"$editor_prompt"
      run_with_timeout "$EDITOR_AGENT_TIMEOUT" \
        claude -p --append-system-prompt-file "$editor_sys" \
        --model "$REVIEW_MODEL" \
        --tools Read Grep Glob Bash --strict-mcp-config --setting-sources project \
        --disable-slash-commands \
        --output-format stream-json --verbose <"$editor_prompt" \
        2>"$EDIT_RAW_FILE.stderr" |
        python3 "$SCRIPT_DIR/stream_format.py" --raw-out "$EDIT_RAW_FILE" \
          --label "${PR_COLOR_START}pr${PR_NUMBER}:editor${PR_COLOR_RESET}" \
          --cost-out "$EDIT_RAW_FILE.cost" ||
        rc=$?
    else
      # ADR 0035, as in the single-agent branch above.
      editor_args="/edit-review $PR_URL --diff $NUMBERED_BASENAME --payload $AUTHOR_INDEXED_BASENAME"
      if [[ -s "$INTENT_FILE" ]]; then
        editor_args="$editor_args --intent $INTENT_BASENAME"
      fi
      run_with_timeout "$EDITOR_AGENT_TIMEOUT" \
        claude -p "$editor_args" \
        --model "$REVIEW_MODEL" \
        --output-format stream-json --verbose \
        2>"$EDIT_RAW_FILE.stderr" |
        python3 "$SCRIPT_DIR/stream_format.py" --raw-out "$EDIT_RAW_FILE" \
          --label "${PR_COLOR_START}pr${PR_NUMBER}:editor${PR_COLOR_RESET}" \
          --cost-out "$EDIT_RAW_FILE.cost" ||
        rc=$?
    fi
    release_claude_slot "$slot"
    exit "$rc"
  ) || edit_rc=$?
  if [[ "$edit_rc" -eq "$TIMEOUT_EXIT" ]]; then
    log_failure "$FAIL_EDIT_TIMEOUT" "$PR_URL" "$HEAD_OID" "editor agent exceeded ${EDITOR_AGENT_TIMEOUT}s"
    exit 1
  fi
  if [[ ! -s "$EDIT_RAW_FILE" ]]; then
    log_failure "$FAIL_EDIT_EMPTY" "$PR_URL" "$HEAD_OID" "editor produced no output"
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
# apply_edits.py exits zero when it downgrades a cosmetic voice miss to a warning,
# so the failure path above never sees it. Forward $EDIT_ERR here too, before
# cleanup() removes $SCRATCH, so a warn-and-post run still names the missed rule
# in the daemon log (the same forwarding the extract step gets at its own gate).
log_degradation_warnings "$EDIT_ERR"

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
if [[ "$new_findings_total" -gt 0 && $DRY_RUN -eq 1 ]]; then
  # Dry-run posts nothing; it reports where the findings that would post live in
  # the preserved scratch for the eval harness to read (contract defined at
  # emit_dryrun_contract in lib.sh). Cost is covered separately by
  # log_total_review_cost below. Safe on stdout: the daemon never runs dry-run.
  log_info "dry-run: ${new_findings_total} finding(s), posting nothing (scratch preserved)"
  emit_dryrun_contract "$new_findings_total"
elif [[ "$new_findings_total" -gt 0 ]]; then
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
elif [[ $DRY_RUN -eq 1 ]]; then
  # Zero findings under dry-run is a real result the harness must record (a recall
  # miss, not a failure), so it reports the same contract with a zero count. The
  # payload still exists (summary plus an empty comments array).
  log_info "dry-run: no findings at $HEAD_OID, posting nothing (scratch preserved)"
  emit_dryrun_contract 0
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
if [[ -n "$LAST_SHA" && $DRY_RUN -eq 0 ]]; then
  log_step "commit-driven resolution"
  resolution || log_info "resolution skipped (non-fatal)"
fi

# Per-push delta counts (ADR 0033 Decision 3): findings posted and threads
# resolved THIS tick, first-hand pipeline state rather than a diff of rendered
# comments. Set only on a re-review (LAST_SHA); an empty array omits the line.
# `fixed` reads resolution's stamps file (ADR 0017/0019), best-effort 0 if absent.
delta_args=()
if [[ -n "$LAST_SHA" && $DRY_RUN -eq 0 ]]; then
  push_fixed="$(jq 'length' "$SCRATCH/.pr-review-stamps.json" 2>/dev/null || printf 0)"
  delta_args=(--new "$new_findings_total" --fixed "$push_fixed")
fi

# Edit the status comment into its terminal state (#60) with the cumulative
# findings index (ADR 0020), read fresh from the PR's threads so it reflects this
# tick's posts and resolves. The headline carries scope, not a per-SHA count: the
# index's rollup replaces that count. All best-effort — a failed fetch or render
# degrades to a headline-and-scope status, never aborts the landed review.
# Skipped wholesale in dry-run: there is no status comment to edit and nothing to
# post, so the fetch and edit are pure side effects with no dry-run value.
if [[ $DRY_RUN -eq 0 ]]; then
  log_step "rendering status index"
  index_threads_file="$SCRATCH/.pr-review-index-threads.json"
  index_block=""
  if fetch_open_review_threads "$BASE_OWNER" "$BASE_REPO" "$PR_NUMBER" >"$index_threads_file"; then
    index_block="$(python3 "$SCRIPT_DIR/findings_index.py" \
      --threads "$index_threads_file" --operator "$OPERATOR" \
      --unanchored "$unanchored_count" --review-url "$review_url" \
      --summary-file "$SUMMARY_FILE" \
      ${delta_args[@]+"${delta_args[@]}"} 2>/dev/null || true)"
  else
    log_info "thread fetch for status index failed (non-fatal)"
  fi
  # Append this tick's reviewed SHA to the trail (ADR 0021): re-parse the prior
  # block (held in STATUS_TRAIL_PRIOR) and fold in HEAD with the reviewed-at time,
  # so the audit record grows by one row rather than being overwritten.
  status_trail_block="$(printf '%s' "$STATUS_TRAIL_PRIOR" |
    python3 "$SCRIPT_DIR/status_trail.py" \
      --add-sha "${HEAD_OID:0:8}" --add-time "$(date -u +'%Y-%m-%d %H:%M UTC')" \
      2>/dev/null || true)"
  reviewed_body="$(render_status_comment \
    "✅ Reviewed $(status_sha_link "$HEAD_REPO_URL" "$HEAD_OID")" \
    "$STATUS_SCOPE" "$STATUS_FILE_COUNT" "$STATUS_FILES" "$index_block" "$status_trail_block" \
    "$HEAD_OID")"
  edit_status_comment "$BASE_OWNER" "$BASE_REPO" "$STATUS_COMMENT_ID" "$reviewed_body"
fi

log_total_review_cost
log_step "done"
