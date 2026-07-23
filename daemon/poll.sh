#!/usr/bin/env bash
# poll.sh — one polling cycle. Lists open PRs across watched repos, dispatches
# review-pr.sh for each eligible PR. daemon/run.sh drives this on the configured
# interval (ADR 0009); this script is a single cycle and does not loop.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=daemon/lib.sh disable=SC1091
source "$SCRIPT_DIR/lib.sh"
REPO_ROOT="$SCRIPT_DIR/.."

# Session-limit backoff (#231), checked before any network call: while the
# subscription quota is exhausted every lens fails instantly, so a cycle can only
# re-flip each open PR's status comment to the same failure. Exit 0, not 1: a
# quota pause is a deliberate skip, and run.sh logs a non-zero cycle as an error.
if PAUSE_UNTIL="$(session_pause_active)"; then
  log_info "session limit pause active, polling resumes at $(format_clock_time "$PAUSE_UNTIL")"
  exit 0
fi

for cmd in gh jq python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log_err "missing '$cmd' on PATH"
    exit 1
  fi
done
if ! gh auth status >/dev/null 2>&1; then
  log_err "gh not authenticated — run 'gh auth login' first"
  exit 1
fi

CONFIG_ERR="$(mktemp -t pr-review-poll-config.XXXXXX)"
# Per-cycle outcome dir: each background dispatch drops an ok/fail marker so the
# end-of-cycle summary tallies reviews without re-querying GitHub.
CYCLE_OUTCOME_DIR="$(mktemp -d -t pr-review-cycle.XXXXXX)"
trap 'rm -f "$CONFIG_ERR"; rm -rf "$CYCLE_OUTCOME_DIR"' EXIT

# Idle-cycle collapse. A cycle is idle when it touches no PR (every watched repo
# has no open, eligible PR). When the prior cycle was idle, suppress this cycle's
# preamble (_LOG_QUIET) and, on a TTY, redraw one "idle (×N)" line in place rather
# than reprint the whole block each interval. The streak persists across cycles —
# each is a fresh poll.sh process — in the state dir.
IDLE_FILE="$(_state_dir)/idle.count"
PRIOR_IDLE=0
if [[ -r "$IDLE_FILE" ]]; then
  read -r PRIOR_IDLE <"$IDLE_FILE" 2>/dev/null || PRIOR_IDLE=0
fi
[[ "$PRIOR_IDLE" =~ ^[0-9]+$ ]] || PRIOR_IDLE=0
CYCLE_TTY=0
if [[ -t 2 ]]; then
  CYCLE_TTY=1
fi
if [[ "$PRIOR_IDLE" -ge 1 ]]; then
  _LOG_QUIET=1
fi
CYCLE_DID_WORK=0

# Finalize a dangling in-place "idle (×N)" line (TTY, no trailing newline left by
# the prior cycle) with one newline before the first fresh output, so real lines
# don't graft onto it. Self-guards, so callers can invoke it unconditionally.
IDLE_LINE_PENDING=0
if [[ "$_LOG_QUIET" -eq 1 && "$CYCLE_TTY" -eq 1 ]]; then
  IDLE_LINE_PENDING=1
fi
flush_idle_line() {
  if [[ "$IDLE_LINE_PENDING" -eq 1 ]]; then
    printf '\n' >&2
    IDLE_LINE_PENDING=0
  fi
}

log_step "loading config"
if ! CONFIG="$(python3 "$SCRIPT_DIR/load_config.py" "$REPO_ROOT" 2>"$CONFIG_ERR")"; then
  cat "$CONFIG_ERR" >&2
  category="$(grep -m1 '^category=' "$CONFIG_ERR" | cut -d= -f2 || true)"
  log_failure "${category:-unknown}" "" "" "config load failed"
  exit 1
fi

REPOS=()
while IFS= read -r r; do REPOS+=("$r"); done < <(jq -r '.repos[]' <<<"$CONFIG")
GITHUB_USER="$(jq -r '.github_user' <<<"$CONFIG")"
GITHUB_APP_ID="$(jq -r '.github_app_id' <<<"$CONFIG")"
REVIEW_OWN_PRS="$(jq -r '.review_own_prs' <<<"$CONFIG")"
OPT_OUT_LABEL="$(jq -r '.opt_out_label' <<<"$CONFIG")"
MAX_PARALLEL="$(jq -r '.max_parallel' <<<"$CONFIG")"

log_info "watched: ${REPOS[*]} (own-PRs: $REVIEW_OWN_PRS, user: $GITHUB_USER, app: $GITHUB_APP_ID, opt-out: ${OPT_OUT_LABEL:-disabled}, parallel: $MAX_PARALLEL)"

# Verify access to every watched repo before doing work. Catches typos and
# missing collaborator access early instead of mid-tick.
log_step "verifying repo access"
for repo in "${REPOS[@]}"; do
  if ! gh repo view "$repo" --json viewerPermission >/dev/null 2>&1; then
    log_err "repo not accessible: $repo — check 'gh auth status' or repo access"
    log_failure "repo-unreachable" "" "" "repo not accessible: $repo"
    exit 1
  fi
done

# Review one PR and record its outcome. Run in the background under the dispatch
# semaphore, so it owns its own state write: the per-PR state file advances only
# on review success (atomic, ADR 0006); a failure leaves it for the next tick.
# Always returns 0 so a failed review never aborts the tick under `set -e` when
# the parent waits on this PID.
dispatch_review() {
  local owner="$1" repo_name="$2" pr_number="$3" head_sha="$4" pr_url="$5"
  shift 5
  if run_with_pr_timeout "review-dispatch" "$pr_url" "$head_sha" \
    bash "$SCRIPT_DIR/review-pr.sh" "$@"; then
    state_write "$owner" "$repo_name" "$pr_number" "$head_sha" 0
    printf 'ok\n' >"$CYCLE_OUTCOME_DIR/${owner}-${repo_name}-${pr_number}"
  else
    log_err "review failed for $pr_url: state untouched, will retry next tick"
    printf 'fail\n' >"$CYCLE_OUTCOME_DIR/${owner}-${repo_name}-${pr_number}"
  fi
  return 0
}

# Bounded-parallel dispatch (#92). Reviews fan out up to MAX_PARALLEL at a time;
# review_head indexes the oldest in-flight PID so a full pool blocks on it to
# free a slot (FIFO). macOS ships bash 3.2 with no `wait -n`, so the bound is a
# head-index over a PID array rather than wait-any. The cap is global across
# repos: the binding constraint is concurrent `claude -p` load, not per-repo.
review_pids=()
review_head=0

for repo in "${REPOS[@]}"; do
  log_step "polling $repo"
  if ! prs_json="$(gh pr list --repo "$repo" --state open \
    --json number,headRefOid,isDraft,author,labels,url 2>/dev/null |
    jq 'sort_by(.number)')"; then
    log_err "cannot list PRs for $repo — skipping this repo"
    continue
  fi

  pr_count="$(jq 'length' <<<"$prs_json")"
  if [[ "$pr_count" -eq 0 ]]; then
    log_info "$repo: no open PRs"
    continue
  fi

  owner="${repo%%/*}"
  repo_name="${repo##*/}"

  while IFS= read -r pr_obj; do
    pr_number="$(jq -r '.number' <<<"$pr_obj")"
    head_sha="$(jq -r '.headRefOid' <<<"$pr_obj")"
    is_draft="$(jq -r '.isDraft' <<<"$pr_obj")"
    author="$(jq -r '.author.login' <<<"$pr_obj")"
    pr_url="$(jq -r '.url' <<<"$pr_obj")"

    if [[ "$is_draft" == "true" ]]; then
      log_info "skipped (draft): $pr_url"
      continue
    fi

    # ADR 0004: own-PR is reviewed by default; opt out via REVIEW_OWN_PRS=false
    # (e.g. team setup where a teammate's daemon covers yours).
    if [[ "$author" == "$GITHUB_USER" && "$REVIEW_OWN_PRS" != "true" ]]; then
      log_info "skipped (own-PR opt-out): $pr_url"
      continue
    fi

    # Per-PR opt-out via label. Empty OPT_OUT_LABEL disables the filter.
    if [[ -n "$OPT_OUT_LABEL" ]]; then
      has_optout="$(jq --arg label "$OPT_OUT_LABEL" \
        '[.labels[]?.name] | any(. == $label)' <<<"$pr_obj")"
      if [[ "$has_optout" == "true" ]]; then
        log_info "skipped (opt-out label '$OPT_OUT_LABEL'): $pr_url"
        continue
      fi
    fi

    # First eligible PR of the cycle: this cycle is not idle. Reset the quiet gate
    # so the work below (and the end-of-cycle tally) prints, and finalize any
    # in-place idle line before the per-PR output starts.
    CYCLE_DID_WORK=1
    _LOG_QUIET=0
    flush_idle_line

    # Reply handling runs every tick regardless of dedup — operators reply
    # independent of HEAD changes. reply-pr.sh exits 0 cheaply when nothing
    # to ack (one gh api call, no scratch clone). Deliberately foreground
    # while reviews background (#198): pooling it alongside the review
    # dispatch below would run this PR's reply and review legs concurrently,
    # and both mutate the same threads (reply acks/resolves, the review's
    # resolution leg stamps/resolves). The cheap no-reply case dominates,
    # and a slow reply is bounded by the per-PR watchdog wrapping it here.
    if ! run_with_pr_timeout "reply-dispatch" "$pr_url" "$head_sha" \
      bash "$SCRIPT_DIR/reply-pr.sh" "$pr_url"; then
      log_err "reply check failed for $pr_url — continuing to review step"
    fi

    # Sentinel-first dedup per ADR 0006. State file is the fallback when the
    # reviews API is unavailable.
    last_sha=""
    if sentinel_sha="$(discover_sentinel_sha "$owner" "$repo_name" "$pr_number" "$GITHUB_USER")"; then
      last_sha="$sentinel_sha"
    else
      sentinel_rc=$?
      state="$(state_read "$owner" "$repo_name" "$pr_number")"
      last_sha="$(jq -r '.last_reviewed_sha // empty' <<<"$state")"
      # ADR 0006 Discovery step 5: API failure must not collapse to first-review
      # when state is also empty — skip and retry on next tick instead.
      if [[ "$sentinel_rc" -eq 2 && -z "$last_sha" ]]; then
        log_info "skipped (discovery API down, no state fallback): $pr_url"
        continue
      fi
    fi
    if [[ -n "$last_sha" && "$head_sha" == "$last_sha" ]]; then
      log_info "skipped (same SHA ${head_sha:0:12}): $pr_url"
      continue
    fi

    log_info "reviewing: $pr_url (HEAD ${head_sha:0:12})"
    # Pass last_sha down so review-pr.sh can scope the diff to changes since
    # the prior review's HEAD. Empty last_sha (first-review) uses the full
    # PR diff.
    review_args=()
    if [[ -n "$last_sha" ]]; then
      review_args+=(--last-sha "$last_sha")
    fi
    review_args+=("$pr_url")
    dispatch_review "$owner" "$repo_name" "$pr_number" "$head_sha" \
      "$pr_url" "${review_args[@]}" &
    review_pids+=($!)
    # Pool full: block on the oldest in-flight review before launching the next.
    if ((${#review_pids[@]} - review_head >= MAX_PARALLEL)); then
      wait "${review_pids[review_head]}" || true
      review_head=$((review_head + 1))
    fi
  done < <(jq -c '.[]' <<<"$prs_json")
done

# Drain reviews still running at the end of the cycle before reporting done, so
# their state writes land and a slow review can't bleed into the next cycle.
wait

if [[ "$CYCLE_DID_WORK" -eq 1 ]]; then
  # The cycle reviewed something: clear the idle streak and close with a tally
  # from the per-dispatch outcome markers instead of a bare "done". The grep is
  # guarded (|| true) so an empty outcome dir can't trip `set -e` via pipefail,
  # and branches use `if`, not `[[ ]] &&`, so a false test never exits under it.
  rm -f "$IDLE_FILE" 2>/dev/null || true
  ok_n=$({ grep -lx ok "$CYCLE_OUTCOME_DIR"/* 2>/dev/null || true; } | wc -l | tr -d ' ')
  fail_n=$({ grep -lx fail "$CYCLE_OUTCOME_DIR"/* 2>/dev/null || true; } | wc -l | tr -d ' ')
  reviewed_n=$((ok_n + fail_n))
  repo_n=${#REPOS[@]}
  repo_noun="repos"
  if [[ "$repo_n" -eq 1 ]]; then
    repo_noun="repo"
  fi
  cycle_summary="cycle done · ${repo_n} ${repo_noun}"
  if [[ "$reviewed_n" -gt 0 ]]; then
    cycle_summary="${cycle_summary} · ${reviewed_n} reviewed"
    if [[ "$fail_n" -gt 0 ]]; then
      cycle_summary="${cycle_summary} · ${fail_n} failed"
    fi
  fi
  log_step "$cycle_summary"
else
  # Idle cycle: bump the streak. The first idle (prior streak 0) still showed the
  # full preamble, so close it with one normal line; consecutive idles were quiet,
  # so redraw a single "(×N)" line in place on a TTY, or stay silent in a log file.
  NEW_IDLE=$((PRIOR_IDLE + 1))
  mkdir -p "$(_state_dir)"
  printf '%s\n' "$NEW_IDLE" >"$IDLE_FILE" 2>/dev/null || true
  if [[ "$PRIOR_IDLE" -lt 1 ]]; then
    log_step "cycle done · no open PRs"
  elif [[ "$CYCLE_TTY" -eq 1 ]]; then
    printf '\r\033[K[pr-review-agent] ◷ idle · no open PRs (×%d)' "$NEW_IDLE" >&2
  fi
fi
