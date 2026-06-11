#!/usr/bin/env bash
# poll.sh — one polling cycle. Lists open PRs across watched repos, dispatches
# review-pr.sh for each eligible PR. daemon/run.sh drives this on the configured
# interval (ADR 0009); this script is a single cycle and does not loop.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=daemon/lib.sh disable=SC1091
source "$SCRIPT_DIR/lib.sh"
REPO_ROOT="$SCRIPT_DIR/.."

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
trap 'rm -f "$CONFIG_ERR"' EXIT

log_step "loading config"
if ! CONFIG="$(python3 "$SCRIPT_DIR/load-config.py" "$REPO_ROOT" 2>"$CONFIG_ERR")"; then
  cat "$CONFIG_ERR" >&2
  category="$(grep -m1 '^category=' "$CONFIG_ERR" | cut -d= -f2 || true)"
  log_failure "${category:-unknown}" "" "" "config load failed"
  exit 1
fi

REPOS=()
while IFS= read -r r; do REPOS+=("$r"); done < <(jq -r '.repos[]' <<<"$CONFIG")
GITHUB_USER="$(jq -r '.github_user' <<<"$CONFIG")"
REVIEW_OWN_PRS="$(jq -r '.review_own_prs' <<<"$CONFIG")"
OPT_OUT_LABEL="$(jq -r '.opt_out_label' <<<"$CONFIG")"

log_info "watched: ${REPOS[*]} (own-PRs: $REVIEW_OWN_PRS, user: $GITHUB_USER, opt-out: ${OPT_OUT_LABEL:-disabled})"

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

for repo in "${REPOS[@]}"; do
  log_step "polling $repo"
  if ! prs_json="$(gh pr list --repo "$repo" --state open \
    --json number,headRefOid,isDraft,author,labels,url 2>/dev/null)"; then
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

    # Reply handling runs every tick regardless of dedup — operators reply
    # independent of HEAD changes. reply-pr.sh exits 0 cheaply when nothing
    # to ack (one gh api call, no scratch clone).
    if ! bash "$SCRIPT_DIR/reply-pr.sh" "$pr_url"; then
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
    if bash "$SCRIPT_DIR/review-pr.sh" "${review_args[@]}"; then
      state_write "$owner" "$repo_name" "$pr_number" "$head_sha" 0
    else
      log_err "review failed for $pr_url — state untouched, will retry next tick"
    fi
  done < <(jq -c '.[]' <<<"$prs_json")
done

log_step "tick done"
