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

for cmd in gh claude jq git python3 openssl curl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log_err "missing '$cmd' on PATH"
    exit 1
  fi
done
# App identity (ADR 0036): a machine with no `gh auth login` is a valid
# deployment; what must exist is the App private key. The App id is checked when
# it is resolved below, once the base repo is known.
if [[ ! -r "$APP_KEY_PATH" ]]; then
  log_err "App private key not readable at $APP_KEY_PATH — place it there or set APP_KEY_PATH (ADR 0036)"
  exit 1
fi

KEEP_SCRATCH=0
DRY_RUN=0
LAST_SHA=""
AT_SHA=""
APP_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-scratch)
      KEEP_SCRATCH=1
      shift
      ;;
    --app-id)
      # The App id, threaded from poll.sh (config-authoritative). The manual
      # one-shot omits it and self-resolves from .env below.
      if [[ $# -lt 2 ]]; then
        log_err "--app-id requires a value"
        exit 1
      fi
      APP_ID="$2"
      shift 2
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

# Set after derive_pr_metadata; leave blank so a log_failure before it still has
# the placeholder field populated.
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

# Sum every claude -p cost sidecar (generation orchestrator, editor, judge-fix)
# and log the total, on success or failure (ADR 0023 dogfood follow-up): each
# was only ever visible as one line per call in the live log, so seeing what a
# review actually cost, or a FAILED review had already burned before it failed,
# meant hand-summing the log by eye.
# Must run before cleanup() removes $SCRATCH (the trap order below
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
  # The routing ledger wants the verdict and the cost together, and this is the
  # only point that holds both (#219). Two runs are excluded rather than
  # recorded: one that died before the diff was classified, whose record would
  # carry no verdict, and any dry run, because the ledger measures what the
  # daemon actually reviews. The eval harness drives `--at-sha`, which is a dry
  # run, so recording those would fill the ledger with the same few fixtures
  # reviewed dozens of times and drown the real PRs it exists to count.
  if [[ -n "${ROUTE_VERDICT:-}" && $DRY_RUN -eq 0 ]]; then
    record_route_observation "$PR_URL" "$HEAD_OID" "$ROUTE_VERDICT" "$total_cost"
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
# rate-limit (retry, §4). Reads run-scoped globals (SCRATCH at HEAD, DIFF_FILE, PRA_* auth,
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
    --threads "$threads_file" --operator "$PRA_BOT_LOGIN_GQL" >"$retry_file"; then
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
  local select_args=(--threads "$threads_file" --diff "$DIFF_FILE" --operator "$PRA_BOT_LOGIN_GQL"
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
    # Directly prompted like the roles and the editor (ADR 0038): ADR 0034
    # measured the slash-command wrapper as pure forwarding overhead. The
    # system prompt is the same for every candidate, so strip it once here;
    # the per-finding prompt file is built inside the loop.
    local judge_sys="$SCRATCH/.pr-review-judge-sys.md"
    awk 'BEGIN { n = 0 } /^---$/ { n++; next } n >= 2 { print }' \
      "$SCRATCH/.claude/agents/review-agent-fix-check.md" >"$judge_sys"
    local i tid path line verdict fixed rationale rc judge_prompt
    for ((i = 0; i < n; i++)); do
      tid="$(jq -r ".[$i].thread_id" "$candidates_file")"
      path="$(jq -r ".[$i].path" "$candidates_file")"
      line="$(jq -r ".[$i].line" "$candidates_file")"
      finding_file="$SCRATCH/.pr-review-finding-${i}.json"
      jq ".[$i] | {path, line, finding_body}" "$candidates_file" >"$finding_file"

      judge_raw="$SCRATCH/.pr-review-judge-${i}.txt"
      # Keyed by the finding's index like the finding file itself, so each
      # candidate's prompt survives the loop as its own post-mortem artifact
      # (the loop is serial; nothing can clobber).
      judge_prompt="$SCRATCH/.pr-review-judge-prompt-${i}.txt"
      {
        printf 'Judge this Finding per your instructions and emit the verdict JSON.\n'
        printf 'The PR under review: %s\n' "$PR_URL"
        printf 'The Finding to judge is at: %s\n' "$(basename "$finding_file")"
      } >"$judge_prompt"
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
          claude -p --append-system-prompt-file "$judge_sys" \
          --model "$REVIEW_MODEL" \
          --tools Read Grep Glob Bash --strict-mcp-config --setting-sources project \
          --disable-slash-commands \
          --output-format stream-json --verbose <"$judge_prompt" \
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
  run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
    python3 "$SCRIPT_DIR/resolve_threads.py" act \
    --notes "$notes_file" --retry "$retry_file" \
    --head-owner "$HEAD_REPO_OWNER" --head-repo "$HEAD_REPO_NAME" --head-sha "$HEAD_OID" ||
    log_info "resolution stamping failed (non-fatal)"
  return 0
}

# App identity (ADR 0036): resolve this process's installation for the base repo
# and warm its token in this main shell, so the wrapped gh calls below (most in
# command substitutions, where a mint would land in a doomed subshell) reuse one
# token. app_auth_init also sets the two bot-login globals the dedup and
# resolution paths match on. --app-id comes from poll.sh; the manual one-shot
# resolves it from .env.
[[ -n "$APP_ID" ]] || APP_ID="$(resolve_tunable GITHUB_APP_ID "$SCRIPT_DIR/../.env")"
if [[ -z "$APP_ID" ]]; then
  log_err "no GITHUB_APP_ID (pass --app-id or set it in .env) — cannot authenticate as the App (ADR 0036)"
  exit 1
fi
if ! app_auth_init "$BASE_OWNER" "$BASE_REPO" "$APP_ID"; then
  log_err "App not installed on ${BASE_OWNER}/${BASE_REPO}, or the installation probe failed"
  exit 1
fi
app_auth_warm || {
  log_err "could not mint an App token for ${BASE_OWNER}/${BASE_REPO}"
  exit 1
}

meta="$(derive_pr_metadata "$PR_URL")" || exit 1
HEAD_REPO_OWNER="$(jq -r '.headRepositoryOwner.login' <<<"$meta")"
HEAD_REPO_NAME="$(jq -r '.headRepository.name' <<<"$meta")"
HEAD_REF="$(jq -r '.headRefName' <<<"$meta")"
HEAD_OID="$(jq -r '.headRefOid' <<<"$meta")"
BASE_REF="$(jq -r '.baseRefName // empty' <<<"$meta")"
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
  existing_sha="$(discover_sentinel_sha "$BASE_OWNER" "$BASE_REPO" "$PR_NUMBER" "$PRA_BOT_LOGIN_REST")" &&
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
run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
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

# Bundle operator's agent defs into the scratch so the dispatch below reads
# them from cwd without requiring target-repo .claude/ setup (ADR 0007).
bundle_operator_agents "$SCRATCH"

# Bare filenames inside the scratch dir. The claude prompt below references the
# diff by basename so a $TMPDIR containing a space can't break the named path.
DIFF_BASENAME=".pr-review-diff.txt"
DIFF_FILE="$SCRATCH/$DIFF_BASENAME"
# The agents read a line-numbered copy so they read `line` off the leading number
# instead of counting hunk lines (ADR 0018). The raw DIFF_FILE stays the
# pipeline's input (anchor_findings split, commit-driven resolution).
NUMBERED_BASENAME=".pr-review-diff-numbered.txt"
NUMBERED_FILE="$SCRATCH/$NUMBERED_BASENAME"
RAW_FILE="$SCRATCH/.pr-review-raw.txt"
# ADR 0038: two fixed generator roles, each unaware of the other. `code` reads
# the diff, the surrounding code, and the repo's conventions, quarantined from
# every author claim; `intent` confronts the claims (description, linked issues,
# commit messages) with the diff. Their raw outputs are unioned and deduped
# before the confidence gate (daemon/merge_findings.py). The set is fixed: the
# REVIEW_LENSES dial died with the lens taxonomy, and only the intent skip below
# changes what runs. Parallel arrays, not an associative array, so this stays
# bash-3.2-safe (ADR 0013's runtime constraint).
LENS_LABELS=(code intent)
LENS_RAW_FILES=(
  "$RAW_FILE"
  "$SCRATCH/.pr-review-raw-intent.txt"
)
# ADR 0035: the intent role reads the change's stated intent alongside the diff,
# so unlike the code role it needs an input file. Bare name like the diff,
# since the role reads it from the scratch cwd.
INTENT_BASENAME=".pr-review-intent.md"
INTENT_FILE="$SCRATCH/$INTENT_BASENAME"

# ADR 0035, extended by ADR 0038: assemble what the change says it does, for the
# intent role to read against the diff. Three rungs. The PR's own title, body,
# and commit messages come free with the metadata fetch above (commit messages
# joined the ladder for the refactor-claim check: a `refactor:` prefix is a
# behavior-preservation claim); the body behind each closing reference costs a
# call per issue and is only there when the author wrote one. A rung that fails
# is named in the file rather than left blank, so the role reads a gap as less
# evidence instead of as license to guess.
build_intent_file() {
  local n owner repo issue_rc
  {
    printf '# What this change says it does\n\n'
    printf '## PR title\n\n%s\n\n' "$(jq -r '.title // ""' <<<"$meta")"
    printf '## PR description\n\n'
    jq -r 'if (.body // "") == "" then "(empty)" else .body end' <<<"$meta"
    printf '\n## Commit messages\n\n'
    jq -r 'if ((.commits // []) | length) == 0 then "(unavailable)" else
      .commits[] | "- `\(.oid[0:7])` \(.messageHeadline)"
        + (if (.messageBody // "") != "" then "\n\n  " + (.messageBody | gsub("\n"; "\n  ")) else "" end)
      end' <<<"$meta"
    printf '\n'
  } >"$INTENT_FILE"

  while read -r n owner repo; do
    [[ -z "$n" ]] && continue
    issue_rc=0
    # Each closing reference carries its own repo, so a cross-repo `Closes` is
    # fetched from where the issue actually lives, not from the PR's base.
    run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" --timeout "${GH_API_CALL_TIMEOUT:-90}" \
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

# The intent role is skipped when the change states nothing to contradict
# (ADR 0035), leaving the code role to run alone. Title alone does not count: it
# is too short to contradict a diff, and counting it would run the role on every
# PR, which is the cost this branch exists to avoid. Commit messages do not count
# either: they exist on every PR, so counting them would defeat the skip, and a
# refactor-claim check with no description to cross-reference rarely beats its
# cost. HTML comments are stripped first so a PR template's boilerplate does not
# read as a description the author wrote.
intent_body_len="$(jq -r '.body // ""' <<<"$meta" |
  python3 -c 'import re,sys; print(len(re.sub(r"<!--.*?-->", "", sys.stdin.read(), flags=re.S).strip()))')"
intent_issue_count="$(jq '.closingIssuesReferences | length' <<<"$meta")"
if [[ "$intent_body_len" -gt 0 || "$intent_issue_count" -gt 0 ]]; then
  build_intent_file
else
  LENS_LABELS=(code)
  LENS_RAW_FILES=("$RAW_FILE")
  log_info "intent role skipped: no description and no linked issue"
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
  run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" --timeout "$GH_API_CALL_TIMEOUT" \
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
  run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" --timeout "$GH_API_CALL_TIMEOUT" \
    gh pr diff "$PR_URL" >"$DIFF_FILE" || diff_rc=$?
  if [[ "$diff_rc" -eq "$TIMEOUT_EXIT" ]]; then
    log_failure "$FAIL_DIFF_FETCH_TIMEOUT" "$PR_URL" "$HEAD_OID" "gh pr diff exceeded ${GH_API_CALL_TIMEOUT}s"
    exit 1
  elif [[ "$diff_rc" -ne 0 ]]; then
    log_failure "$FAIL_DIFF_FETCH_FAILED" "$PR_URL" "$HEAD_OID" "gh pr diff exited $diff_rc"
    exit 1
  fi
fi

# Observation only, no behaviour change (#219). Whether routing the code role
# away from prose-only diffs is worth building is undecided: the roles run in
# parallel inside one orchestrator, so dropping one saves tokens but almost no
# wall-clock, and on the eval corpus only one of five deliberately-trivial PRs
# even qualifies. Recording the classification against real PRs is what decides
# it, and it costs nothing to record. Read the count of these lines against the
# per-review cost logged below before acting on them.
if python3 "$SCRIPT_DIR/route_diff.py" "$DIFF_FILE"; then
  ROUTE_VERDICT="behaviour"
else
  ROUTE_VERDICT="prose-only"
  log_info "prose-only diff: the code role would be skippable here (#219, not skipped)"
fi

# Line-numbered diff for the agents (ADR 0018, layer A).
python3 "$SCRIPT_DIR/anchor_findings.py" number "$DIFF_FILE" >"$NUMBERED_FILE"

# Post (or reuse) the durable status comment before the multi-minute review, so
# the operator sees the PR is being looked at and the scope being read (#60).
# Scope comes from the same diff: file list plus commit range (full PR first,
# <last-sha>..HEAD on re-review). One comment per PR, reused across ticks.
STATUS_FILES="$(python3 "$SCRIPT_DIR/diff_paths.py" "$DIFF_FILE")"
STATUS_FILE_COUNT="$(printf '%s' "$STATUS_FILES" | grep -c . || true)"
# Scope names the commit range this review covered (#102): base..HEAD on a first
# review, the compare link <last>..HEAD on a re-review. Pass LAST_SHA only when
# the diff was actually scoped to it (a fetch failure falls back to the full
# diff). BASE_REF is named unconditionally, not just when it differs from the
# default branch: knowing the default would need its own `gh repo view` call, and
# a stacked PR (base is another open PR's branch, not main) is common enough in a
# solo/incremental workflow that the operator should not have to guess which diff
# they are seeing. status_scope_link places it as the left endpoint or as context.
if [[ $diff_scoped -eq 1 ]]; then
  STATUS_SCOPE="$(status_scope_link "$HEAD_REPO_URL" "$LAST_SHA" "$HEAD_OID" "$BASE_REF")"
else
  STATUS_SCOPE="$(status_scope_link "$HEAD_REPO_URL" "" "$HEAD_OID" "$BASE_REF")"
fi
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
  STATUS_COMMENT_ID="$(find_status_comment "$BASE_OWNER" "$BASE_REPO" "$PR_NUMBER" "$PRA_BOT_LOGIN_REST")"
  if [[ -n "$STATUS_COMMENT_ID" ]]; then
    STATUS_TRAIL_PRIOR="$(status_comment_body "$BASE_OWNER" "$BASE_REPO" "$STATUS_COMMENT_ID" |
      python3 "$SCRIPT_DIR/status_trail.py" 2>/dev/null || true)"
  fi

  # The "Reviewing…" render carries the prior trail unchanged — this tick's SHA is
  # not reviewed yet; the terminal render below folds it in.
  reviewing_body="$(render_status_comment \
    "$(render_status_headline "$PRA_BOT_LOGIN_GQL" reviewing \
      "$(status_sha_link "$HEAD_REPO_URL" "$HEAD_OID")" "$HEAD_OID")" \
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
# can push this further. Under Shape C (#299) this bounds the whole orchestrator
# session, not one role: the roles run in parallel inside it, so the budget is
# still sized to the slowest role plus the orchestrator's own dispatch overhead,
# and the CLI offers no per-subagent cap to restore the per-role bound. A role's
# payload written before the timeout survives it (see the orchestrator exit
# handling below).
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

# ADR 0038 amended (#299, Shape C): the roles run as parallel subagents inside
# ONE orchestrator `claude -p` session, spawned via the Agent tool from the
# agent definitions the bundle placed in the scratch's .claude/agents/. The
# roles stay unaware of each other (each subagent gets its own context and its
# own task prompt), but the process-level fan-out is gone, and with it three
# things this block used to give per role: its own timeout (REVIEW_AGENT_TIMEOUT
# now bounds the whole orchestrator; the CLI has no per-subagent cap), its own
# .cost sidecar (stream-json exposes only the session aggregate), and its own
# labeled activity stream (one label covers the stage).
#
# Each role writes its own fenced payload to its named scratch file, so
# merge_findings.py keeps reading N raw files exactly as under the process
# fan-out, and no payload round-trips through the orchestrator's generation
# (a re-emission would risk truncation or paraphrase on a long review). The
# orchestrator is told to spawn, wait, and report: never to review, retry, or
# touch a payload file.
#
# Slot accounting: the orchestrator is one process running role_count hidden
# API sessions, so it charges one slot per role (acquire_claude_slots,
# all-or-nothing to avoid hold-and-wait deadlock between concurrent
# orchestrators, clamped to the pool size). MAX_PARALLEL still only bounds
# review-pr.sh *processes*; the slot pool keeps bounding concurrent sessions.
role_count="${#LENS_LABELS[@]}"

ORCH_RAW="$SCRATCH/.pr-review-orchestrator.txt"
ORCH_PROMPT="$SCRATCH/.pr-review-orchestrator-prompt.txt"
{
  printf 'You are a dispatch orchestrator. Spawn one subagent per role listed below with the Agent tool, issuing ALL the Agent calls in a single message so the roles run in parallel. Pass each role its task prompt verbatim, exactly as written between the BEGIN/END markers.\n\n'
  printf 'Do not review the PR yourself, do not read the diff, and never read, summarize, or write any payload file a role owns.\n\n'
  role_i=0
  while [[ "$role_i" -lt "$role_count" ]]; do
    role_label="${LENS_LABELS[$role_i]}"
    role_raw_basename="$(basename "${LENS_RAW_FILES[$role_i]}")"
    # shellcheck disable=SC2016  # the backticks are a markdown code span in the prompt, not a command substitution
    printf -- '--- Role %d: agent type `review-agent-%s` ---\n' "$((role_i + 1))" "$role_label"
    printf 'BEGIN TASK PROMPT\n'
    printf 'Review this PR per your instructions.\n'
    printf 'The line-numbered diff is at: %s\n' "$NUMBERED_BASENAME"
    # ADR 0035: the intent role gets the second side of its comparison. The
    # code role has only the diff, which is the quarantine ADR 0038 names.
    if [[ "$role_label" == "intent" ]]; then
      printf 'What the change says it does is at: %s\n' "$INTENT_BASENAME"
    fi
    # The diff goes by path, not inlined: inlining it and saying "reason over
    # this" made the model treat the context as complete and skip opening the
    # callers, which is the verify step's whole substance (ADR 0022).
    printf 'Read it, then investigate the surrounding code with your tools '
    printf '(open the callers, trace the data flow) to verify each candidate before scoring.\n'
    printf 'Write your complete output, the ```json fenced payload your instructions require, to a new file named %s. ' "$role_raw_basename"
    printf 'Your final reply must not restate the payload; one line confirming the write is enough.\n'
    printf 'END TASK PROMPT\n\n'
    role_i=$((role_i + 1))
  done
  printf 'Wait for every role to complete. A role that errors or writes nothing is tolerated: never retry it, never write its file yourself, and let its siblings finish. When all roles are done, reply with one line per role: <label>: ok or failed.\n'
} >"$ORCH_PROMPT"

log_step "running review orchestrator (${LENS_LABELS[*]}) via claude -p"
orch_rc=0
(
  # One slot per hidden role session; released together after the stage.
  slots="$(acquire_claude_slots "$role_count" "review orchestrator")"
  # Explicit exit: the `( ... ) || orch_rc=$?` wrapper suppresses errexit inside
  # this subshell, so an unguarded failed cd would run claude in the daemon's
  # own repo and the roles would write payloads there.
  cd "$SCRATCH" || {
    release_claude_slots "$slots"
    exit 1
  }
  rc=0
  # `claude`'s own stderr (e.g. its non-interactive workspace-trust notice,
  # expected every run since a fresh scratch clone is never pre-trusted) goes
  # to a sidecar file so the primary log stays legible.
  # --tools carries the union of what the role agents need plus Agent: a
  # subagent's effective tool set is the intersection of its frontmatter list
  # and the parent session's --tools, so a tool missing here is silently
  # unavailable to every role no matter what the agent file grants.
  # --permission-mode acceptEdits: non-interactive claude auto-denies the
  # Write permission prompt, so without this every role completes its review
  # and then fails to land its payload file. acceptEdits auto-approves file
  # writes inside the session's working directory, which is the scratch.
  # The payload files are deliberately NOT pre-created: Write refuses to
  # overwrite an existing file it has not Read.
  # No --append-system-prompt-file: the roles' system prompts come from the
  # bundled .claude/agents/ definitions, loaded via --setting-sources project;
  # the prompt file above is the orchestrator's whole instruction.
  run_with_timeout "$REVIEW_AGENT_TIMEOUT" \
    claude -p \
    --model "$REVIEW_MODEL" \
    --tools Agent Read Write Bash Grep Glob WebFetch \
    --permission-mode acceptEdits \
    --strict-mcp-config --setting-sources project \
    --disable-slash-commands \
    --output-format stream-json --verbose <"$ORCH_PROMPT" \
    2>"$ORCH_RAW.stderr" |
    python3 "$SCRIPT_DIR/stream_format.py" --raw-out "$ORCH_RAW" \
      --label "${PR_COLOR_START}pr${PR_NUMBER}:review${PR_COLOR_RESET}" \
      --cost-out "$ORCH_RAW.cost" ||
    rc=$?
  release_claude_slots "$slots"
  exit "$rc"
) || orch_rc=$?

# A bad orchestrator exit is not fatal by itself: a role that finished writing
# its payload before a timeout still counts, exactly as a surviving lens did
# under the process fan-out. The per-role check logs what is missing, and
# merge_findings.py rolls a total loss up to all-lenses-failed (ADR 0023).
if [[ "$orch_rc" -eq "$TIMEOUT_EXIT" ]]; then
  log_info "review orchestrator exceeded ${REVIEW_AGENT_TIMEOUT}s; continuing with whatever payloads landed"
elif [[ "$orch_rc" -ne 0 ]]; then
  log_info "review orchestrator exited ${orch_rc}; continuing with whatever payloads landed"
fi
role_i=0
while [[ "$role_i" -lt "$role_count" ]]; do
  # Ensure the file exists before merge reads it: a role that never landed its
  # payload (timeout, subagent failure) leaves nothing, and merge_findings.py
  # crashes on a missing path where it tolerates an empty one (ADR 0023).
  [[ -e "${LENS_RAW_FILES[$role_i]}" ]] || : >"${LENS_RAW_FILES[$role_i]}"
  if [[ ! -s "${LENS_RAW_FILES[$role_i]}" ]]; then
    log_info "${LENS_LABELS[$role_i]} role produced no payload; continuing without it"
  fi
  role_i=$((role_i + 1))
done

# Confidence gate threshold (ADR 0022): resolve from env, then .env, and export
# so merge_findings.py's os.environ read sees it. The daemon never sources .env
# wholesale, so without this the operator's .env dial would silently no-op and
# the gate would sit at extract_json.py's default. An unset value stays unexported
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
# --session-limit-probe (#299): on a quota hit the roles' payload files stay
# empty and the sentinel lands in the orchestrator transcript, so merge probes
# it to keep #231's pause classification working under the orchestrator shape.
if ! python3 "$SCRIPT_DIR/merge_findings.py" --no-style \
  --session-limit-probe "$ORCH_RAW" "${LENS_RAW_FILES[@]}" \
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
    # The editor runs in the same directly-prompted shape as the roles, for the
    # same reason (ADR 0034's measurement: its slash command only dispatched the
    # editor subagent). It keeps its tools because ADR 0016's verify-at-HEAD
    # step needs them, and because a tools-free editor could not reliably emit
    # its decisions JSON on a complex draft.
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
      # file exists only when the role ran, and only that role emits a finding
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
  # Reviews submit immediately as a COMMENT review under the bot identity (ADR
  # 0036 decision 6): no pending draft, no own-vs-others fork.
  log_step "submitting review"
  post_args+=(--app-id "$APP_ID" --installation-id "$PRA_INSTALLATION_ID" --app-slug "$PRA_BOT_LOGIN_GQL")
  if ! bash "$SCRIPT_DIR/create-review.sh" "${post_args[@]}" >"$POST_OUT" 2>"$POST_ERR"; then
    cat "$POST_ERR" >&2
    category="$(extract_category "$POST_ERR")"
    log_failure "$category" "$PR_URL" "$HEAD_OID" "review POST failed"
    exit 1
  fi
  # Report the landed review by id instead of dumping the raw JSON. A parse miss
  # (unexpected shape) degrades to no id, never an error — the review did land.
  review_id="$(jq -r '.id // empty' "$POST_OUT" 2>/dev/null || true)"
  # html_url anchors the status index's "outside the diff" pointer at this review,
  # the home of any relocated finding (ADR 0005, ADR 0020 Decision 4).
  review_url="$(jq -r '.html_url // empty' "$POST_OUT" 2>/dev/null || true)"
  log_ok "submitted review${review_id:+ #$review_id}"
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
      --threads "$index_threads_file" --operator "$PRA_BOT_LOGIN_GQL" \
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
      --add-sha "${HEAD_OID:0:7}" --add-time "$(date -u +'%Y-%m-%d %H:%M UTC')" \
      2>/dev/null || true)"
  # Gate verdict for the themed head-line (ADR 0036 4a): "you shall not pass" when
  # any bot finding is still open after this tick's resolution, else "you shall
  # pass". Binary, not a count — the tally stays in the index rollup (ADR 0020).
  # Counts prior open threads (from the freshly-fetched index threads) and this
  # tick's new findings; a failed thread fetch degrades to the new-findings count.
  open_findings="$(jq --arg op "$PRA_BOT_LOGIN_GQL" \
    '[.[] | select(.root_author == $op and ((.is_resolved // false) | not))] | length' \
    "$index_threads_file" 2>/dev/null || printf 0)"
  review_state="pass"
  if [[ "${open_findings:-0}" -gt 0 || "${new_findings_total:-0}" -gt 0 ]]; then
    review_state="block"
  fi
  reviewed_body="$(render_status_comment \
    "$(render_status_headline "$PRA_BOT_LOGIN_GQL" "$review_state" \
      "$(status_sha_link "$HEAD_REPO_URL" "$HEAD_OID")" "$HEAD_OID")" \
    "$STATUS_SCOPE" "$STATUS_FILE_COUNT" "$STATUS_FILES" "$index_block" "$status_trail_block" \
    "$HEAD_OID")"
  edit_status_comment "$BASE_OWNER" "$BASE_REPO" "$STATUS_COMMENT_ID" "$reviewed_body"
fi

log_total_review_cost
log_step "done"
