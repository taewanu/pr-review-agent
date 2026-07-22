# shellcheck shell=bash
# lib.sh — shared helpers sourced by other daemon scripts. Not executable on its own.

# Per-PR log context. When set, log_* carry the coloured repo#pr prefix instead
# of [pr-review-agent], so interleaved parallel lines are attributable.
# review-pr.sh / reply-pr.sh each process one PR and set it; poll.sh / run.sh
# leave it unset, keeping the plain prefix for cycle-level lines.
_LOG_PR_REPO=""
_LOG_PR_NUM=""

# Cycle-level quiet gate. When set to 1, the plain-prefix path of log_info/log_step
# is suppressed, so a run of idle polling cycles doesn't reprint the same preamble
# (poll.sh sets it when the prior cycle was idle). Per-PR context lines and
# log_err/log_failure are never gated: identity and problems always surface.
_LOG_QUIET=0

# log_set_pr_context <repo-name> <pr-number>
log_set_pr_context() {
  _LOG_PR_REPO="$1"
  _LOG_PR_NUM="$2"
}

log_info() {
  if [[ -n "$_LOG_PR_REPO" ]]; then
    log_pr "$_LOG_PR_REPO" "$_LOG_PR_NUM" "$LOG_GLYPH_STEP" "$*"
  elif [[ "$_LOG_QUIET" != "1" ]]; then
    printf '[pr-review-agent] %s\n' "$*" >&2
  fi
}

log_err() {
  if [[ -n "$_LOG_PR_REPO" ]]; then
    log_pr "$_LOG_PR_REPO" "$_LOG_PR_NUM" "$LOG_GLYPH_FAIL" "$*"
  else
    printf '[pr-review-agent] ERROR: %s\n' "$*" >&2
  fi
}

# log_step <message> — a phase line with elapsed. $SECONDS is the calling
# process's own clock: in review-pr.sh that is the per-PR review time (each PR
# runs in its own process), in poll.sh/run.sh the cycle clock.
log_step() {
  if [[ -n "$_LOG_PR_REPO" ]]; then
    log_pr "$_LOG_PR_REPO" "$_LOG_PR_NUM" "$LOG_GLYPH_STEP" "$*" "$SECONDS"
  elif [[ "$_LOG_QUIET" != "1" ]]; then
    printf '[pr-review-agent] %s (+%ds)\n' "$*" "${SECONDS}" >&2
  fi
}

# log_ok <message> — a success phase line (✓ in per-PR mode). Same elapsed rule
# as log_step.
log_ok() {
  if [[ -n "$_LOG_PR_REPO" ]]; then
    log_pr "$_LOG_PR_REPO" "$_LOG_PR_NUM" "$LOG_GLYPH_OK" "$*" "$SECONDS"
  else
    printf '[pr-review-agent] %s (+%ds)\n' "$*" "${SECONDS}" >&2
  fi
}

# log_degradation_warnings <captured-stderr-file>
# Forwards quality-degradation warnings a stage prints on its SUCCESS path to the
# daemon log, because review-pr.sh reads each captured stderr only on failure:
# merge-skip / finding-skip / confidence-gate from merge_findings.py (#196), and
# voice-warning from apply_edits.py (a cosmetic style miss downgraded to warn-and-
# post). Without this a lens silently dropped to four (#196), or a missed voice
# rule left no signal before cleanup() removed the scratch. Lives in lib.sh, not
# inline, so test_degradation_warnings.py can source it (same rationale as
# wait_for_lens_pids, ADR 0026).
log_degradation_warnings() {
  local stderr_file="$1" line
  [[ -r "$stderr_file" ]] || return 0
  while IFS= read -r line; do
    log_info "$line"
  done < <(grep -E '^(merge-skip|finding-skip|confidence-gate|voice-warning)' "$stderr_file" || true)
}

# log_failure <category> <pr-url> <head-sha> <reason>
# Positional fields per ADR 0005 so log scrapers don't re-parse prose.
log_failure() {
  local category="$1" url="$2" sha="$3" reason="$4"
  # Record the latest failure category so an EXIT-trap consumer can surface it on
  # the status comment (#180); harmless in callers that don't read it.
  # shellcheck disable=SC2034  # consumed by review-pr.sh's failure trap (#180)
  LAST_FAILURE_CATEGORY="$category"
  printf '[pr-review-agent] failure: %s pr=%s sha=%s reason=%s\n' \
    "$category" "$url" "$sha" "$reason" >&2
}

# --- Output styling ----------------------------------------------------------
# The daemon log has two render targets: a colour TTY (an operator watching a
# foreground run) and a plaintext file (.daemon.log, and the ADR 0005 failure
# scraper). Colour and glyphs decorate; identity always lives in the line TEXT,
# so a colour-stripped reader loses nothing. Styling is emitted only when stderr
# is a TTY and NO_COLOR is unset; PR_LOG_COLOR=always|never forces it for tests.
_log_color_enabled() {
  case "${PR_LOG_COLOR:-auto}" in
    always) return 0 ;;
    never) return 1 ;;
    *) [[ -t 2 && -z "${NO_COLOR:-}" ]] ;;
  esac
}

# _sgr <code...> — emit an ANSI SGR sequence (e.g. `_sgr 1 36`) when colour is
# on, nothing when off. Pair every styling call with `_sgr 0` to reset.
_sgr() {
  _log_color_enabled || return 0
  local IFS=';'
  printf '\033[%sm' "$*"
}

# Per-PR prefix palette. Excludes red (31, reserved for the ✗ failure glyph) and
# green (32, reserved for ✓ success) so a PR colour never reads as a status.
# Bright variants extend the set before it wraps.
_LOG_PR_PALETTE=(36 35 33 34 96 95 94 93)

# Status glyphs. Plain Unicode, not colour, so they survive a piped log; colour,
# when on, is layered around them by the caller.
LOG_GLYPH_STEP='•'
LOG_GLYPH_OK='✓'
LOG_GLYPH_FAIL='✗'

# pr_color_index <key> — map a per-PR key (e.g. "repo#pr") to an index into
# _LOG_PR_PALETTE, in [0, ${#_LOG_PR_PALETTE[@]}); picks the PR prefix colour.
# Hashing keeps a PR's colour stable across cycles; with 8 buckets two PRs in
# flight can collide, tolerable because the repo#pr text disambiguates anyway.
pr_color_index() {
  local n=${#_LOG_PR_PALETTE[@]}
  local sum
  sum=$(cksum <<<"$1" | cut -d' ' -f1)
  printf '%s' "$((sum % n))"
}

# pr_prefix <repo-name> <pr-number> — render the "repo#pr" identity prefix,
# coloured by pr_color_index when colour is on, plain when off. The TEXT is
# identical in both modes; only the surrounding SGR differs, so a colourless log
# still disambiguates every line.
pr_prefix() {
  local repo="$1" pr="$2" tag idx
  tag="${repo}#${pr}"
  idx="$(pr_color_index "$tag")"
  _sgr "${_LOG_PR_PALETTE[idx]}"
  printf '%s' "$tag"
  _sgr 0
}

# log_pr <repo-name> <pr-number> <glyph> <message> [elapsed-seconds]
# Emit one styled per-PR line: "  repo#pr  <glyph> message   +Ns". The coloured
# prefix tells interleaved parallel lines apart; the elapsed suffix is dimmed and
# omitted when empty.
log_pr() {
  local repo="$1" pr="$2" glyph="$3" msg="$4" elapsed="${5:-}"
  {
    printf '  %s  %s %s' "$(pr_prefix "$repo" "$pr")" "$glyph" "$msg"
    if [[ -n "$elapsed" ]]; then
      _sgr 2
      printf '   +%ss' "$elapsed"
      _sgr 0
    fi
    printf '\n'
  } >&2
}

# Exit status the shell reports when a process is killed by SIGALRM (128 + 14).
# run_with_timeout returns this on timeout; callers branch on it to route to the
# ADR 0005 failure path instead of parsing the truncated output.
# shellcheck disable=SC2034  # consumed by reply-pr.sh / review-pr.sh
readonly TIMEOUT_EXIT=142

# run_with_timeout <seconds> <command...>
# Caps a command's wall-clock runtime. macOS has no coreutils `timeout`, so we
# lean on perl's alarm: it arms a SIGALRM timer, then `exec`s the command into
# the same process. The timer survives exec (it is kernel state, not perl's
# memory) while exec resets SIGALRM to its default of terminate, so an
# over-running command dies at the cap. Returns the command's own exit status,
# or $TIMEOUT_EXIT on timeout. Orphaned grandchildren may briefly outlive the
# kill, acceptable for a backstop: stdout closes and the tick fails over anyway.
run_with_timeout() {
  local secs="$1"
  shift
  perl -e 'alarm shift; exec @ARGV or exit 127' "$secs" "$@"
}

# resolve_tunable <KEY> <dotenv-path>
# Resolve an operator dial: the exported environment wins, then the KEY=VALUE
# line in the .env file, else empty (the caller supplies the default). Prints the
# value. Uses grep, not `source`, so a stray .env line can't run as code (the
# same parse as run.sh's POLL_INTERVAL and bin/install.sh). The daemon never
# sources .env wholesale, so a Python subprocess reading os.environ only sees a
# dial the shell resolved here and exported.
resolve_tunable() {
  local key="$1" dotenv="$2" val
  val="${!key:-}"
  if [[ -z "$val" && -r "$dotenv" ]]; then
    # `|| true` scopes tolerance to grep alone: a missing key (exit 1) is the
    # normal absent-dial case and must resolve to empty, not abort the function
    # under the daemon's `set -euo pipefail`. A genuinely broken downstream pipe
    # still propagates.
    val="$({ grep -E "^${key}=" "$dotenv" || true; } | head -1 | cut -d= -f2- | tr -d '"'\''')"
  fi
  printf '%s' "$val"
}

# resolve_review_model <dotenv-path>
# Resolve the reasoning model every daemon claude -p pins (#209), printing it.
# The default lives here alone: it reaches the agents as a --model argument the
# callers expand, never as an environment variable, so a script that resolved it
# separately would be free to drift onto a different model. Unlike the dials
# resolve_tunable serves, an empty value is not a usable default here (`--model
# ""` is a broken flag), so this one defaults in the shell rather than downstream.
resolve_review_model() {
  local model
  model="$(resolve_tunable REVIEW_MODEL "$1")"
  printf '%s' "${model:-claude-opus-4-8}"
}

# warn_env_drift <dotenv-path> <template-path>
# One boot-time line naming every key the template carries but the live .env
# lacks — those knobs silently run on their code defaults (#201). The concrete
# failure: CLAUDE_SLOT_POOL_SIZE (ADR 0023) never synced into a pre-existing
# .env pinned lens concurrency at 3 instead of the recommended 10, visible
# only as slot-wait-dominated 7-9 minute reviews. Read-only and non-blocking:
# unreadable files no-op, and a commented-out key counts as absent because it
# also runs the code default.
warn_env_drift() {
  local env_file="$1" template_file="$2" key missing=()
  [[ -r "$env_file" && -r "$template_file" ]] || return 0
  while IFS= read -r key; do
    # A key exported in the environment is live (resolve_tunable resolves env
    # before .env), not a code default — don't flag it as drift.
    [[ -n "${!key:-}" ]] && continue
    grep -qE "^${key}=" "$env_file" || missing+=("$key")
  done < <(sed -nE 's/^([A-Za-z_][A-Za-z0-9_]*)=.*/\1/p' "$template_file")
  ((${#missing[@]} > 0)) || return 0
  log_info "config drift: ${missing[*]} in templates/.env.example but not in .env — running on code defaults; add the key(s) to .env if that is not intended"
}

# Outer per-PR watchdog cap (#121). run_with_timeout bounds only one inner
# `claude -p` call; the network steps around it (`gh repo clone`, the per-PR
# `git fetch`, `gh pr diff`, the `gh api` posts) were unbounded, so a stalled
# fetch after a laptop sleep once froze the whole serial loop for ~10h. This
# caps a per-PR step end-to-end.
#
# review-pr.sh's 5 lenses run in parallel against a shared claude_slot pool
# (ADR 0023 revision), so this is not sized to their strict sequential sum.
# Dogfooding the multi-lens design on a real codebase showed lens durations of
# 178-262s even in the least-contended case, which is why REVIEW_AGENT_TIMEOUT/
# EDITOR_AGENT_TIMEOUT rose to 600s; the realistic (not worst-case) shape is
# one lens wave (~300-500s under real contention, well under the 600s cap)
# plus the editor (~300-400s) plus clone/fetch/post overhead (~300s) ≈
# 1000-1200s. 1800 adds a real margin for cross-PR contention on the shared
# pool when MAX_PARALLEL > 1, not the full pathological worst case (every
# concurrent PR maxing out simultaneously), which is unbounded for any shared
# resource pool. A run that hits this bound under genuine heavy contention
# fails and retries next cycle (poll.sh leaves state untouched on failure)
# rather than being data loss, so this value trades some detection latency for
# not chasing an unbounded worst case. Override for tests.
#
# The resolution leg (#197) also runs inside this same wrapped process, after
# the post: up to RESOLVE_TOUCHED_CAP + RESOLVE_UNTOUCHED_CAP serial judge-fix
# calls, each slot-pooled and bounded by FIX_CHECK_AGENT_TIMEOUT (180s) —
# those caps exist so this leg stays a bounded slice of the same budget. A
# kill here is costlier than one mid-review: STATUS_DONE=1 has already stamped
# the sentinel at HEAD, so the next tick skips on "same SHA" and the round's
# remaining resolution work waits for the next push.
readonly PER_PR_TIMEOUT="${PER_PR_TIMEOUT:-1800}"

# run_with_pr_timeout <failure-category> <pr-url> <head-sha> <command...>
# Wraps one per-PR step (review-pr.sh / reply-pr.sh dispatch) in PER_PR_TIMEOUT
# so a hang in any network step fails over to the next tick instead of wedging
# the loop (#121). On timeout, emits the ADR 0005 structured failure line and
# returns $TIMEOUT_EXIT; otherwise passes the command's own exit status through,
# so a genuine non-zero is never misread as a timeout. The broad backstop:
# whatever stalls — git, gh, claude, DNS — is bounded here.
run_with_pr_timeout() {
  local category="$1" url="$2" sha="$3"
  shift 3
  local rc=0
  run_with_timeout "$PER_PR_TIMEOUT" "$@" || rc=$?
  if [[ "$rc" -eq "$TIMEOUT_EXIT" ]]; then
    log_failure "${category}-timeout" "$url" "$sha" "per-PR step exceeded ${PER_PR_TIMEOUT}s"
  fi
  return "$rc"
}

# arm_git_stall_timeout
# Exports git's low-speed abort thresholds so an https clone/fetch over a stalled
# connection (laptop sleep, network drop) aborts with an error in ~30s instead of
# hanging forever (#121, the exact failure seen). Layered under the poll.sh
# per-PR watchdog: this kills the common https stall cleanly so the script's own
# cleanup runs (scratch removed, lock released), while the watchdog backstops
# everything the env can't reach (ssh transport, gh api, DNS). Exported, not just
# set, so the git child processes inherit it. Override either threshold for tests.
arm_git_stall_timeout() {
  export GIT_HTTP_LOW_SPEED_LIMIT="${GIT_HTTP_LOW_SPEED_LIMIT:-1000}"
  export GIT_HTTP_LOW_SPEED_TIME="${GIT_HTTP_LOW_SPEED_TIME:-30}"
}

# Pure predicate over a GitHub compare `status` ($1): true when the prior SHA is
# an ancestor of HEAD, so an incremental diff is valid. "ahead" (HEAD has new
# commits on top) and "identical" (same commit) are fast-forwards; "diverged"
# (force-push/rebase) and "behind" are not. An unknown or empty status (a failed
# or empty compare API call) is treated as not-a-fast-forward so callers fall
# back to the full PR diff rather than scoping on an unverified ancestry (#149).
_status_is_fast_forward() {
  [[ "$1" == "ahead" || "$1" == "identical" ]]
}

# True when the prior-reviewed SHA ($2) is an ancestor of HEAD ($3) in the head
# repo ($1, "owner/name"), i.e. the branch advanced by a fast-forward so an
# incremental `$2..HEAD` diff still reflects only the PR's new commits. False
# after a force-push or rebase: the tips diverged, so `$2..HEAD` surfaces
# unrelated base commits and cancels the PR's own change (#123).
#
# Asks GitHub's compare API rather than the local clone. The per-PR clone is
# shallow (--depth=1), so it lacks the history for `merge-base --is-ancestor` to
# prove ancestry and every clean fast-forward read as a force-push, silently
# defeating the incremental scope (#149). The server computes `status` from full
# history; a failed or empty response falls through to not-a-fast-forward, so
# callers take the safe full PR diff.
is_fast_forward() {
  local status
  status="$(gh api "repos/$1/compare/$2...$3" --jq '.status' 2>/dev/null)"
  _status_is_fast_forward "$status"
}

# Decide whether to defer an operator reply thread because the fix commit it
# names is not in the reviewed HEAD's history yet. The race: the operator
# commits the fix, replies naming that SHA, and pushes, but this reply pass
# fetched HEAD before the push landed. Verifying the pre-fix code would wrongly
# push back on a correct fix; deferring lets a later cycle verify once the push
# arrives (poll.sh leaves the thread unaddressed, so it is redispatched). ADR 0028.
#
# Args: $1 the operator reply body, $2 the head repo (owner/name).
# Returns 0 to defer this thread, 1 to verify it now (the pre-existing behavior).
# Defers only when the claimed commit is absent from the repo, the race signal.
# A commit that exists but is not an ancestor of HEAD (a later rebase moved the
# fix onto an equivalent commit) still carries its fix into HEAD's tree, so the
# agent verifies against HEAD as before rather than deferring forever on it.
reply_defers_on_unreachable_fix() {
  local reply_body="$1" head_repo="$2" sha=""
  # A deliberately-referenced commit: 7-40 hex either backtick-delimited
  # (`115dbb3`, or the `sha:Lnn` link form) or in a github commit/blob URL.
  # Prose hex ("the deadbeef case") is not shaped like either, so it never
  # matches and the thread verifies now instead of deferring forever.
  # shellcheck disable=SC2016  # backticks are literal regex delimiters, not a subshell
  local re='`([0-9a-fA-F]{7,40})(:[Ll][0-9]|`)|/(commit|blob)/([0-9a-fA-F]{7,40})'
  if [[ "$reply_body" =~ $re ]]; then
    sha="${BASH_REMATCH[1]:-${BASH_REMATCH[4]}}"
    sha="$(printf '%s' "$sha" | tr '[:upper:]' '[:lower:]')"
  fi

  [[ -n "$sha" ]] || return 1
  commit_exists "$head_repo" "$sha" && return 1
  return 0
}

# True when $2 resolves to a commit in repo $1 (owner/name). Distinguishes an
# unpushed fix SHA (the race, absent: defer) from one that exists but diverged
# after a rebase (present: verify against HEAD). ADR 0028.
commit_exists() {
  gh api "repos/$1/commits/$2" --jq '.sha' >/dev/null 2>&1
}

# State tracking for same-SHA dedup. One file per PR. Layered behind the
# sentinel-based dedup (ADR 0006) as a fallback when GitHub's reviews API is
# unavailable.
#
# Override $PR_REVIEW_STATE_DIR for tests.
_state_dir() {
  printf '%s' "${PR_REVIEW_STATE_DIR:-$HOME/.pr-review-agent}"
}

_state_path() {
  printf '%s/%s-%s-%s.json' "$(_state_dir)" "$1" "$2" "$3"
}

# state_read <owner> <repo> <pr-number>
# Emits the state JSON on stdout. Missing file → '{}' and exit 0 (absence is
# the normal case for first-ever-tick on a PR, not an error).
state_read() {
  local path
  path="$(_state_path "$1" "$2" "$3")"
  if [[ -r "$path" ]]; then
    cat "$path"
  else
    printf '{}\n'
  fi
}

# state_write <owner> <repo> <pr-number> <head-sha> <review-id>
# Atomic write. Creates the state dir if missing. The mktemp + mv pattern keeps
# concurrent ticks from observing a half-written file.
state_write() {
  local owner="$1" repo="$2" pr="$3" sha="$4" review_id="$5"
  local dir path tmp ts
  dir="$(_state_dir)"
  path="$(_state_path "$owner" "$repo" "$pr")"
  ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  mkdir -p "$dir"
  tmp="$(mktemp "$dir/.pr-review-state.XXXXXX")"
  if ! jq -n \
    --arg sha "$sha" \
    --argjson review_id "$review_id" \
    --arg ts "$ts" \
    '{last_reviewed_sha: $sha, review_id: $review_id, ts_iso: $ts}' \
    >"$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  mv "$tmp" "$path"
}

# --- GitHub App authentication (ADR 0036) ------------------------------------
#
# The daemon posts as `<app>[bot]` using an installation token minted from the
# App private key. Only the second leg can use gh: `gh api` sends
# `Authorization: token`, which the App endpoints reject, so the JWT leg is
# openssl plus curl.
#
# run_with_app_token is the only sanctioned way to hold a token; the minting and
# caching behind it are private, because the ways to hold a token wrong all look
# right (see its comment). Installation discovery is public and credential-free:
# `app_installation_id` and `discover_missing_installations` answer "is the App
# installed here" from a JWT alone, and #241 calls both.

# Where the App private key lives. Environment-only for now; whether it also
# earns a .env key is #241's call, alongside the rest of the App config.
APP_KEY_PATH="${APP_KEY_PATH:-$HOME/.pr-review-agent/app.pem}"

# _b64url
# base64url per the JWS spec: standard base64, then the two alphabet
# substitutions and the padding strip.
_b64url() {
  openssl base64 -A | tr '+/' '-_' | tr -d '='
}

# _app_jwt <app-id>
# Prints a JWT signed with the App private key, accepted by the App-level
# endpoints. `iat` is backdated 60s because GitHub rejects a token issued ahead
# of its own clock, and a laptop drifting a few seconds forward is ordinary.
# `exp` stays inside GitHub's 10-minute ceiling: this JWT exists only to mint an
# installation token, never as a credential the daemon holds.
_app_jwt() {
  local app_id="$1" now header payload signing_input signature stderr_capture
  now="$(date +%s)"

  header="$(printf '{"alg":"RS256","typ":"JWT"}' | _b64url)"
  payload="$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$app_id" | _b64url)"
  signing_input="${header}.${payload}"

  # openssl's own message separates a missing key from an unreadable or malformed
  # one, which "could not sign" alone cannot.
  stderr_capture="$(mktemp -t pr-review-app.XXXXXX)"
  if ! signature="$(printf '%s' "$signing_input" | openssl dgst -sha256 -sign "$APP_KEY_PATH" 2>"$stderr_capture" | _b64url)"; then
    log_err "could not sign the App JWT with ${APP_KEY_PATH}: $(<"$stderr_capture")"
    rm -f "$stderr_capture"
    return 1
  fi
  rm -f "$stderr_capture"

  printf '%s.%s' "$signing_input" "$signature"
}

# _mint_installation_token <app-id> <installation-id>
# Exchanges a JWT for an installation token, printing "<token> <expires_at>".
# curl rather than gh because of the Bearer requirement above. The response is
# parsed rather than trusted: a 4xx returns an error body with no `token` key,
# which would otherwise print as an empty credential and fail far from here.
_mint_installation_token() {
  local app_id="$1" installation_id="$2" jwt response token expires_at
  local api_message stderr_capture

  jwt="$(_app_jwt "$app_id")" || return 1

  stderr_capture="$(mktemp -t pr-review-app.XXXXXX)"
  if ! response="$(curl -sS -X POST \
    --connect-timeout 10 --max-time 30 \
    -H "Authorization: Bearer ${jwt}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/app/installations/${installation_id}/access_tokens" 2>"$stderr_capture")"; then
    log_err "installation token request failed for installation ${installation_id}: $(<"$stderr_capture")"
    rm -f "$stderr_capture"
    return 1
  fi
  rm -f "$stderr_capture"

  # Validate the body parses before reading any field. Guarded because an
  # unparseable body (proxy HTML, an empty 502) exits jq non-zero, and a bare
  # assignment would take the daemon down with `set -e` instead of returning.
  if ! jq -e . >/dev/null 2>&1 <<<"$response"; then
    log_err "installation token response was not JSON for installation ${installation_id}"
    return 1
  fi

  # One field per call rather than a packed line. A `@tsv` row read back with
  # `IFS=$'\t'` silently mis-assigns: tab is whitespace, so `read` collapses a
  # leading run of it, and an error body with no `token` shifted `.message` into
  # the token slot. That printed GitHub's error text as a credential at exit 0.
  token="$(jq -r '.token // empty' <<<"$response")"
  expires_at="$(jq -r '.expires_at // empty' <<<"$response")"

  if [[ -z "$token" ]]; then
    # The error message is safe to log; the token never is.
    api_message="$(jq -r '.message // empty' <<<"$response")"
    log_err "installation token rejected: ${api_message:-no message in response}"
    return 1
  fi

  printf '%s %s' "$token" "$expires_at"
}

# app_installation_id <owner> <repo> <app-id>
# Prints the installation id for one repository. Exit codes separate the two
# outcomes a caller must treat differently:
#   0: installed (stdout has the id)
#   1: not installed (GitHub answers 404), the skip-with-a-warning case
#   2: the call itself failed (network, 5xx), which is evidence of neither
# The 404 is exactly the missing-installation signal, so the check falls out of a
# call the daemon already needs to make (ADR 0036 decision 4).
app_installation_id() {
  local owner="$1" repo="$2" app_id="$3" jwt response id http_code
  local stderr_capture

  jwt="$(_app_jwt "$app_id")" || return 2

  stderr_capture="$(mktemp -t pr-review-app.XXXXXX)"
  if ! response="$(curl -sS -w '\n%{http_code}' \
    --connect-timeout 10 --max-time 30 \
    -H "Authorization: Bearer ${jwt}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${owner}/${repo}/installation" 2>"$stderr_capture")"; then
    log_err "installation probe failed for ${owner}/${repo}: $(<"$stderr_capture")"
    rm -f "$stderr_capture"
    return 2
  fi
  rm -f "$stderr_capture"

  http_code="$(tail -1 <<<"$response")"
  case "$http_code" in
    200) ;;
    404) return 1 ;;
    *)
      # Logged because rc=2 leaves the repo in the watch list: without a line
      # here a 500 sweep is silent, and the operator sees only the per-PR
      # failures that follow.
      log_err "installation probe for ${owner}/${repo} answered ${http_code}"
      return 2
      ;;
  esac

  if ! id="$(sed '$d' <<<"$response" | jq -r '.id // empty' 2>/dev/null)"; then
    log_err "installation probe for ${owner}/${repo} returned unparseable JSON"
    return 2
  fi
  [[ -n "$id" ]] || return 2
  printf '%s' "$id"
}

# discover_missing_installations <app-id> <repo>...
# Prints every watched repo the App is not installed on, one per line, and logs
# one line naming them. Both, because the caller needs the list to filter its
# watch list and probing twice would double the network calls; the name says
# discover for the same reason `discover_sentinel_sha` does.
#
# Call it once at daemon start, alongside `warn_env_drift` in run.sh, not per
# cycle: a missing installation is a permanent per-repo state (ADR 0036
# decision 4), and run.sh already documents the rule ("Boot-time, not per-tick").
# Installing the App on a watched repo mid-run therefore needs a restart, which
# is the trade for not probing every repo every cycle.
#
# A probe that fails outright leaves the repo in the watch list, since "could not
# check" is not "not installed".
discover_missing_installations() {
  local app_id="$1" repo owner name rc missing=()
  shift

  for repo in "$@"; do
    owner="${repo%%/*}"
    name="${repo##*/}"
    app_installation_id "$owner" "$name" "$app_id" >/dev/null && continue
    rc=$?
    if [[ "$rc" -eq 1 ]]; then
      missing+=("$repo")
      printf '%s\n' "$repo"
    fi
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    log_err "App not installed on: ${missing[*]} (skipped until you install it)"
  fi
}

# _rfc3339_to_epoch <timestamp>
# Prints a GitHub RFC3339 timestamp as Unix seconds. BSD `date` first (the
# documented macOS target), GNU second so the test suite runs on Linux CI.
# Prints nothing on a shape neither parses, leaving the caller to decide.
_rfc3339_to_epoch() {
  date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null ||
    date -u -d "$1" +%s 2>/dev/null ||
    true
}

# Treat a token as expired this many seconds early, covering the gap the naive
# check leaves: one that passes validation and then dies mid-request, failing a
# write that may not be safe to retry. An internal safety margin rather than an
# operator dial, so it carries no .env key; tests override it by assignment.
GH_TOKEN_REFRESH_MARGIN=300

# _gh_token <app-id> <installation-id>
# Sets _GH_TOKEN_VALUE to a live installation token. Private: run_with_app_token
# is the only sanctioned caller, because a token in a variable is a token someone
# can pass somewhere it must not go.
#
# It assigns rather than prints so the cache survives. A caller reading it as
# `$(_gh_token ...)` runs it in a subshell, and command substitution forks: the
# cache assignment dies with the subshell while only stdout comes back, so every
# call re-mints. The function looked correct in isolation and was, which is why
# a direct-call test passed while no wrapped call ever warmed the cache.
#
# Cached in a shell variable for the life of the process, keyed by installation
# because a token minted for one is not valid for another. Disk would survive
# across the scripts one cycle invokes, but it writes a bearer credential with
# write access to every installed repository to the filesystem to save a handful
# of mints. A shell variable is never exported, so the `claude -p` children that
# inherit the environment do not inherit the credential.
_gh_token() {
  local app_id="$1" installation_id="$2"
  local cache_key cached now token expires_at expiry minted

  # Cleared first so a failed mint cannot leave the previous call's token behind
  # for a caller that forgets to check the exit code.
  _GH_TOKEN_VALUE=""

  # Indirect expansion needs a name, so sanitize the id into one.
  cache_key="_GH_TOKEN_CACHE_${installation_id//[^0-9A-Za-z]/_}"
  now="$(date +%s)"
  cached="${!cache_key:-}"

  # Cache entry is "<token> <unix-expiry>".
  if [[ -n "$cached" ]]; then
    token="${cached%% *}"
    expiry="${cached##* }"
    if ((now + GH_TOKEN_REFRESH_MARGIN < expiry)); then
      _GH_TOKEN_VALUE="$token"
      return 0
    fi
  fi

  minted="$(_mint_installation_token "$app_id" "$installation_id")" || return 1
  token="${minted%% *}"
  expires_at="${minted##* }"

  # An unparseable expiry falls back to GitHub's documented hour rather than
  # failing the call: the token itself is valid, and the margin above absorbs
  # the imprecision.
  expiry="$(_rfc3339_to_epoch "$expires_at")"
  [[ -n "$expiry" ]] || expiry=$((now + 3600))

  # Assigns globals that outlive this function but are never exported.
  printf -v "$cache_key" '%s %s' "$token" "$expiry"
  _GH_TOKEN_VALUE="$token"
}

# run_with_app_token <app-id> <installation-id> <command> [args...]
# Runs one command with the installation token in its environment and nowhere
# else. The daemon's only sanctioned way to authenticate as the App.
#
# It exists because the obvious spelling is wrong in a way that is invisible:
#
#     GH_TOKEN="$(_gh_token 1 2)" gh api ...     # do not
#
# An assignment prefix cannot fail a command. When the mint fails that runs gh
# with GH_TOKEN empty at exit 0, so gh falls back to the operator's stored login
# and posts under the human identity the App replaces. This checks first and
# aborts, which is the loud failure ADR 0036 decision 5 asks for.
#
# Takes a command rather than printing a value because gh is invoked from Python
# too (`daemon/resolution.py` and friends shell out without an `env=`).
#
# The command is allowlisted rather than `claude` being banned, because a ban
# fails open: `run_with_timeout 5 claude` and `bash review-pr.sh` both slip past
# a name check on the first word, and the second hands the token to every agent
# the script starts.
#
# `python3` is on the list and is itself an interpreter, so it is admitted only
# as a runner for this repo's own helpers: a `.py` path, never `-c`, which would
# take arbitrary code and could spawn an agent as readily as bash.
#
# The limit worth knowing: an allowed command is trusted not to spawn an agent
# itself. `gh` cannot, and the daemon's Python helpers do not. Widening this
# extends that trust, which is why test_app_auth.py pins the accepted shapes.
#
# A timeout wraps this, not the other way round, since `run_with_timeout` is not
# allowed: `run_with_timeout 30 run_with_app_token ...`. That ordering also
# bounds the mint, which the inner spelling would leave unbounded.
run_with_app_token() {
  local app_id="$1" installation_id="$2" token command
  shift 2

  if [[ $# -eq 0 ]]; then
    log_err "run_with_app_token needs a command to run"
    return 1
  fi

  command="$(basename -- "$1")"
  case "$command" in
    gh) ;;
    python3)
      if [[ "${2:-}" != *.py ]]; then
        log_err "run_with_app_token refuses 'python3 ${2:-}': the interpreter may hold an App token only to run a .py helper, never inline code (ADR 0036 decision 5)"
        return 1
      fi
      ;;
    *)
      log_err "run_with_app_token refuses '${command}': only gh and the daemon's .py helpers may hold an App token (ADR 0036 decision 5). Wrap a timeout outside this call, not inside."
      return 1
      ;;
  esac

  if ! _gh_token "$app_id" "$installation_id" || [[ -z "$_GH_TOKEN_VALUE" ]]; then
    log_err "no App installation token for installation ${installation_id}: refusing to run '$1' unauthenticated"
    return 1
  fi
  token="$_GH_TOKEN_VALUE"

  GH_TOKEN="$token" "$@"
}

# flatten_pages
# Merges `gh api --paginate` stdout into one flat JSON array. `--paginate`
# emits one array per 100-item page, concatenated (`[...][...]`), which is not
# itself valid input for a single-array jq parse (#195). Keep this out of the
# gh command substitution so gh's own exit code stays observable.
flatten_pages() {
  jq -s 'add // []'
}

# discover_sentinel_sha <owner> <repo> <pr-number> <login>
# Reads the prior reviewed SHA from the most recent operator-authored review or
# PR comment carrying the ADR 0006 sentinel. Comments are scanned too so the SHA
# survives a submit-modal body-wipe that strips it from the review (#49). Exit
# codes drive the caller's fallback policy:
#   0 — sentinel found (stdout has SHA)
#   1 — APIs ok, nothing carries a sentinel (stdout empty)
#   2 — the reviews API call failed (network, 5xx, auth break)
# 1 lets the caller fall through to state and accept first-review on empty;
# 2 demands the caller skip the tick instead of misreading "could not check"
# as "no prior review". `--paginate` keeps PRs with >30 reviews/comments from
# silent truncation.
discover_sentinel_sha() {
  local owner="$1" repo="$2" pr="$3" login="$4"
  local reviews_json comments_json stderr_capture
  stderr_capture="$(mktemp -t pr-review-discover.XXXXXX)"
  if ! reviews_json="$(gh api --paginate "repos/${owner}/${repo}/pulls/${pr}/reviews" 2>"$stderr_capture")"; then
    log_err "sentinel discovery: gh api .../pulls/${pr}/reviews failed: $(<"$stderr_capture")"
    rm -f "$stderr_capture"
    return 2
  fi
  # Comments are best-effort: a failure here degrades to reviews-only rather
  # than escalating to a return-2 skip, since reviews (the primary source)
  # already succeeded.
  if ! comments_json="$(gh api --paginate "repos/${owner}/${repo}/issues/${pr}/comments" 2>"$stderr_capture")"; then
    log_err "sentinel discovery: gh api .../issues/${pr}/comments failed (degrading to reviews-only): $(<"$stderr_capture")"
    comments_json="[]"
  fi
  rm -f "$stderr_capture"
  reviews_json="$(flatten_pages <<<"$reviews_json")"
  comments_json="$(flatten_pages <<<"$comments_json")"
  # One timeline across both sources, newest first; a still-pending review's
  # submitted_at is null, so fall back to created_at. Comments use updated_at,
  # not created_at: the status comment is created once on first review and
  # edited every tick after (ADR 0024), so its creation time would always sort
  # as ancient next to any later-submitted review, hiding a freshly
  # re-embedded sentinel behind a stale one from an old review object.
  local sha
  sha="$(jq -rn --argjson reviews "$reviews_json" --argjson comments "$comments_json" --arg login "$login" '
    ([$reviews[]  | {login: .user.login, ts: (.submitted_at // .created_at), body}]
     + [$comments[] | {login: .user.login, ts: (.updated_at // .created_at), body}])
    | [.[] | select(.login == $login)]
    | sort_by(.ts) | reverse
    | [.[] | .body | capture("<!-- pr-review-agent:sha:(?<sha>[0-9a-f]{40}) -->"; "")? | .sha]
    | first // ""
  ')"
  if [[ -n "$sha" ]]; then
    printf '%s\n' "$sha"
    return 0
  fi
  return 1
}

# Per-PR review lock. ADR 0008's own-PR auto-submit dropped the implicit "one
# pending review per PR" constraint that serialized concurrent reviews (a second
# pending POST was rejected with 422). A submitted COMMENT review has no such
# limit, so a manual review-pr.sh run overlapping a daemon tick would double-post
# (#67); these helpers reinstate serialization locally. macOS has no `flock`, so
# the lock is a noclobber lockfile: `set -o noclobber` makes `>` fail when the
# file exists, and the holder PID and epoch are written in the same redirect, so
# a half-written lock is never observable.
#
# Override $PR_REVIEW_LOCK_STALE_SECONDS for tests.
_lock_path() {
  printf '%s/%s-%s-%s.lock' "$(_state_dir)" "$1" "$2" "$3"
}

# acquire_pr_lock <owner> <repo> <pr-number>
# Non-blocking. On success prints the lock path on stdout and returns 0. Returns
# 1 if a live lock is already held. A lock whose holder process is gone, or that
# has outlived PR_REVIEW_LOCK_STALE_SECONDS (default 1800, longer than any real
# review), is treated as abandoned and reclaimed.
acquire_pr_lock() {
  local owner="$1" repo="$2" pr="$3"
  local dir lockfile stale holder created now
  dir="$(_state_dir)"
  mkdir -p "$dir"
  lockfile="$(_lock_path "$owner" "$repo" "$pr")"
  stale="${PR_REVIEW_LOCK_STALE_SECONDS:-1800}"
  if (
    set -o noclobber
    printf '%s %s\n' "$$" "$(date +%s)" >"$lockfile"
  ) 2>/dev/null; then
    printf '%s\n' "$lockfile"
    return 0
  fi
  # Held. Reclaim only if the holder is gone or the lock outlived the stale
  # window; an empty read (a lock mid-acquisition) counts as live. The
  # reclaim-then-recreate races if two runs hit the same stale lock at once,
  # acceptable since that only follows a prior holder's crash.
  read -r holder created <"$lockfile" 2>/dev/null || true
  now="$(date +%s)"
  if { [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; } ||
    { [[ -n "$created" ]] && ((now - created > stale)); }; then
    rm -f "$lockfile"
    if (
      set -o noclobber
      printf '%s %s\n' "$$" "$(date +%s)" >"$lockfile"
    ) 2>/dev/null; then
      printf '%s\n' "$lockfile"
      return 0
    fi
  fi
  return 1
}

# release_pr_lock <lock-path>
# Removes the lock. No-op on an empty path so a cleanup trap can call it
# unconditionally even if the run exited before acquiring.
release_pr_lock() {
  local lockfile="${1:-}"
  [[ -n "$lockfile" ]] && rm -f "$lockfile"
  return 0
}

# Global slot pool bounding concurrent `claude -p` calls (ADR 0023 revision):
# every lens and the editor acquire one before dispatch and release it after.
# Same noclobber + stale-reclaim mechanism as the per-PR lock above, extended
# to CLAUDE_SLOT_POOL_SIZE numbered slots instead of one PR-scoped lock. Shared
# automatically across every concurrently-running review-pr.sh process: the
# slot files live on disk, not in one process's memory, so poll.sh's
# MAX_PARALLEL (how many review-pr.sh processes exist) and this pool (how many
# claude -p calls run) compose without either script knowing about the other.
#
# Unlike acquire_pr_lock, this is blocking: a lens should wait for a slot, not
# skip the PR on contention. Override $CLAUDE_SLOT_POOL_SIZE,
# $CLAUDE_SLOT_STALE_SECONDS, $CLAUDE_SLOT_POLL_SECONDS for tests.
_slot_path() {
  printf '%s/claude-slot-%s.lock' "$(_state_dir)" "$1"
}

# _try_claim_slot <slot-number>
# One non-blocking attempt at a single numbered slot. Same reclaim rule as
# acquire_pr_lock: a lock whose holder is dead, or that outlived the stale
# window, is reclaimed rather than waited on forever.
_try_claim_slot() {
  local slot="$1" path stale holder created now
  path="$(_slot_path "$slot")"
  if (
    set -o noclobber
    printf '%s %s\n' "$$" "$(date +%s)" >"$path"
  ) 2>/dev/null; then
    printf '%s\n' "$path"
    return 0
  fi
  stale="${CLAUDE_SLOT_STALE_SECONDS:-1800}"
  read -r holder created <"$path" 2>/dev/null || true
  now="$(date +%s)"
  if { [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; } ||
    { [[ -n "$created" ]] && ((now - created > stale)); }; then
    rm -f "$path"
    if (
      set -o noclobber
      printf '%s %s\n' "$$" "$(date +%s)" >"$path"
    ) 2>/dev/null; then
      printf '%s\n' "$path"
      return 0
    fi
  fi
  return 1
}

# The slot-pool sibling of load_config's MAX_PARALLEL ceiling (#161): a typo'd
# CLAUDE_SLOT_POOL_SIZE=50 is a fan-out mistake, not intent, since each unit is
# one concurrent `claude -p` call (#200).
readonly CLAUDE_SLOT_POOL_CEILING=16

# _slot_pool_size
# CLAUDE_SLOT_POOL_SIZE validated to a floor of 1 and the ceiling above
# (#200). Unlike load_config's hard-fail at startup, this is a runtime env
# read inside a live review, so a bad value degrades with a stderr warning
# instead of crashing the tick (the CONFIDENCE_THRESHOLD contract): garbage or
# 0 (which would spin the acquire loop forever, no slot 1..0 exists) falls
# back to the default; an over-ceiling value clamps.
_slot_pool_size() {
  local raw="${CLAUDE_SLOT_POOL_SIZE:-3}" size
  if [[ "$raw" =~ ^[0-9]+$ ]] && ((10#$raw >= 1)); then
    size=$((10#$raw))
  else
    log_info "claude-slot: ignoring invalid CLAUDE_SLOT_POOL_SIZE='${raw}', using default 3"
    size=3
  fi
  if ((size > CLAUDE_SLOT_POOL_CEILING)); then
    log_info "claude-slot: clamping CLAUDE_SLOT_POOL_SIZE=${size} to ceiling ${CLAUDE_SLOT_POOL_CEILING}"
    size=$CLAUDE_SLOT_POOL_CEILING
  fi
  printf '%s\n' "$size"
}

# acquire_claude_slot [label]
# Blocks until one of CLAUDE_SLOT_POOL_SIZE slots is free (polling every
# CLAUDE_SLOT_POLL_SECONDS), then prints the claimed slot's lock path on
# stdout. No internal timeout: the caller's own outer watchdog (PER_PR_TIMEOUT)
# is the backstop if every slot stays held past its stale window without ever
# clearing, matching how other unbounded waits in this pipeline are bounded.
#
# label, when given, logs which slot was claimed and how long the wait was
# (nothing printed when it's empty, e.g. from a caller that doesn't pass one).
# A dogfood round showed lens completion times of 35-519s with no way to tell
# whether a slow one was genuinely complex work or blocked on slot contention;
# this splits the two apart. The log call is safe inside `slot="$(...)"`:
# log_info always writes to stderr, never stdout, so it can't corrupt the
# captured slot path.
acquire_claude_slot() {
  local label="${1:-}" dir pool_size poll_interval i slot_path start_ts waited suffix
  dir="$(_state_dir)"
  mkdir -p "$dir"
  pool_size="$(_slot_pool_size)"
  poll_interval="${CLAUDE_SLOT_POLL_SECONDS:-2}"
  start_ts="$(date +%s)"
  while true; do
    i=1
    while ((i <= pool_size)); do
      if slot_path="$(_try_claim_slot "$i")"; then
        if [[ -n "$label" ]]; then
          waited=$(($(date +%s) - start_ts))
          suffix=""
          ((waited > 0)) && suffix=" (waited ${waited}s)"
          log_info "${label}: acquired slot ${i}/${pool_size}${suffix}"
        fi
        printf '%s\n' "$slot_path"
        return 0
      fi
      i=$((i + 1))
    done
    sleep "$poll_interval"
  done
}

# release_claude_slot <slot-lock-path>
# Removes the slot lock. No-op on an empty path, same rationale as
# release_pr_lock: a cleanup path can call this unconditionally.
release_claude_slot() {
  local lockfile="${1:-}"
  [[ -n "$lockfile" ]] && rm -f "$lockfile"
  return 0
}

# wait_for_lens_pids
# Waits on every backgrounded lens PID in dispatch order, regardless of any
# earlier lens's outcome (ADR 0026). Reads the caller's lens_count,
# LENS_LABELS, LENS_RAW_FILES, and lens_pids arrays as globals, matching how
# review-pr.sh already shares this state; extracted into lib.sh, rather than
# left inline, so a test can source this file and assert the loop reaches
# lens_i == lens_count even when an earlier lens times out or writes nothing,
# the exact case that used to `exit 1` before the later lenses were reaped.
# shellcheck disable=SC2154  # lens_count/LENS_LABELS/LENS_RAW_FILES/lens_pids set by the caller
wait_for_lens_pids() {
  local lens_i=0 lens_label lens_raw lens_rc
  while [[ "$lens_i" -lt "$lens_count" ]]; do
    lens_label="${LENS_LABELS[$lens_i]}"
    lens_raw="${LENS_RAW_FILES[$lens_i]}"
    lens_rc=0
    wait "${lens_pids[$lens_i]}" || lens_rc=$?
    if [[ "$lens_rc" -eq "$TIMEOUT_EXIT" ]]; then
      log_info "$lens_label lens exceeded ${REVIEW_AGENT_TIMEOUT}s; continuing without it"
    elif [[ ! -s "$lens_raw" ]]; then
      log_info "$lens_label lens produced no output; continuing without it"
    fi
    lens_i=$((lens_i + 1))
  done
}

# emit_dryrun_contract <count>
# The machine-readable contract review-pr.sh --dry-run prints so the eval harness
# can locate the findings that would post, without the review being posted. This
# is the single source for the contract; call sites reference it rather than
# restate it. `dryrun_*=<path|count>` follows the repo's `key=value` signal
# convention (extract_category/extract_truncated_count parse
# `category=`/`truncated_count=`), but on stdout, not the stderr those Python
# steps use, because this is review-pr.sh's own report rather than a subprocess's.
# dryrun_payload is the full edited review object (summary plus comments), so the
# harness needs no separate summary/anchored/unanchored locators. Reads the
# run-scoped PAYLOAD_FILE the caller set; extracted into lib.sh, not left inline
# in review-pr.sh, so a test can source this file and assert the fields (ADR 0026
# precedent, as wait_for_lens_pids above).
# shellcheck disable=SC2154  # PAYLOAD_FILE set by the caller
emit_dryrun_contract() {
  printf 'dryrun_payload=%s\n' "$PAYLOAD_FILE"
  printf 'dryrun_count=%s\n' "$1"
}

# Daemon singleton + heartbeat for the run.sh polling loop (ADR 0009). The loop
# is the scheduling driver in place of a launchd StartInterval timer; the
# singleton stops two loops (a foreground run.sh and the KeepAlive one) from both
# driving ticks, and the heartbeat makes liveness observable. Same noclobber +
# dead-holder-reclaim mechanism as the per-PR lock above, minus the stale window:
# a loop is long-lived by design, so a live holder is never aged out — only a
# dead one (a SIGKILLed loop that skipped its cleanup trap) is reclaimed.
#
# Override $PR_REVIEW_STATE_DIR for tests.
_daemon_pid_path() {
  printf '%s/daemon.pid' "$(_state_dir)"
}

_heartbeat_path() {
  printf '%s/daemon.heartbeat' "$(_state_dir)"
}

# acquire_daemon_singleton
# Non-blocking. Prints the pidfile path and returns 0 if no live loop holds it
# (reclaiming a dead holder's file); returns 1 if a live run.sh already holds it.
acquire_daemon_singleton() {
  local dir pidfile holder
  dir="$(_state_dir)"
  mkdir -p "$dir"
  pidfile="$(_daemon_pid_path)"
  if (
    set -o noclobber
    printf '%s\n' "$$" >"$pidfile"
  ) 2>/dev/null; then
    printf '%s\n' "$pidfile"
    return 0
  fi
  # Held. Reclaim only if the holder is gone; an empty read (a pidfile
  # mid-write) counts as live and is left alone.
  read -r holder <"$pidfile" 2>/dev/null || true
  if [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; then
    rm -f "$pidfile"
    if (
      set -o noclobber
      printf '%s\n' "$$" >"$pidfile"
    ) 2>/dev/null; then
      printf '%s\n' "$pidfile"
      return 0
    fi
  fi
  return 1
}

# release_daemon_singleton <pidfile>
# Removes the pidfile. No-op on an empty path so a cleanup trap can call it
# unconditionally even if the loop exited before acquiring.
release_daemon_singleton() {
  local pidfile="${1:-}"
  [[ -n "$pidfile" ]] && rm -f "$pidfile"
  return 0
}

# write_heartbeat
# Atomically stamps the heartbeat file with the current epoch, once per loop
# cycle. Liveness is then "now - heartbeat < a couple of intervals"; a stale
# stamp means the loop is dead or a tick is wedged.
write_heartbeat() {
  local dir hb tmp
  dir="$(_state_dir)"
  hb="$(_heartbeat_path)"
  mkdir -p "$dir"
  tmp="$(mktemp "$dir/.heartbeat.XXXXXX")" || return 1
  if printf '%s\n' "$(date +%s)" >"$tmp"; then
    mv "$tmp" "$hb"
  else
    rm -f "$tmp"
    return 1
  fi
}

# Session-limit backoff (#231). When every lens hits the subscription quota, the
# next cycle cannot pass either, so review-pr.sh records a deadline here and
# poll.sh honours it before doing any work. A file, because the pause must
# outlive both the failing review process and a daemon restart, and because both
# run shapes (foreground run.sh, launchd KeepAlive) then honour it with no extra
# wiring.
_session_pause_path() {
  printf '%s/session-pause.epoch' "$(_state_dir)"
}

# Fallback pause when merge_findings.py could not resolve a reset time from the
# sentinel. One hour bounds both errors: it collapses a retry storm that
# otherwise runs every cycle for hours, while a pause that overshoots an
# already-passed reset costs at most one hour of review latency. Overridable
# like every other tunable in this file.
SESSION_PAUSE_FALLBACK_SECONDS="${SESSION_PAUSE_FALLBACK_SECONDS:-3600}"

# session_pause_write [deadline-epoch]
# Records the pause and prints the deadline it stored. merge_findings.py resolves
# the sentinel's reset time, so an empty or non-numeric argument means it could
# not, and the fixed fallback applies.
session_pause_write() {
  local deadline="${1:-}"
  if ! [[ "$deadline" =~ ^[0-9]+$ ]]; then
    deadline="$(($(date +%s) + SESSION_PAUSE_FALLBACK_SECONDS))"
  fi
  mkdir -p "$(_state_dir)"
  printf '%s\n' "$deadline" >"$(_session_pause_path)"
  printf '%s\n' "$deadline"
}

# session_pause_active
# Returns 0 and prints the deadline epoch while a recorded pause is still in the
# future; returns 1 otherwise, clearing a passed or unreadable file so a stale
# deadline can never wedge polling.
session_pause_active() {
  local path deadline now
  path="$(_session_pause_path)"
  [[ -r "$path" ]] || return 1
  read -r deadline <"$path" 2>/dev/null || deadline=""
  now="$(date +%s)"
  if [[ "$deadline" =~ ^[0-9]+$ && "$deadline" -gt "$now" ]]; then
    printf '%s\n' "$deadline"
    return 0
  fi
  rm -f "$path"
  return 1
}

# format_clock_time <epoch>
# Renders an epoch as a local HH:MM for a log line. Tries the BSD flag then the
# GNU one, and prints the raw epoch if neither works, so a log line is never lost
# to a date-flag difference.
format_clock_time() {
  date -r "$1" '+%H:%M' 2>/dev/null ||
    date -d "@$1" '+%H:%M' 2>/dev/null ||
    printf '%s' "$1"
}

# bundle_operator_agents <scratch-dir>
# Copies operator's agent + slash-command files from this repo's .claude/ into
# the scratch clone's .claude/ so claude -p (which loads from cwd) finds them
# without requiring target-repo setup (ADR 0007). Target-repo files (already
# present in the scratch from the clone) win via the `[[ -e dst ]] || cp`
# guard — a repo can ship its own override.
bundle_operator_agents() {
  local scratch="$1"
  local repo_root
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  mkdir -p "$scratch/.claude/agents" "$scratch/.claude/commands"
  local f base
  for f in "$repo_root/.claude/agents/review-agent-"*.md; do
    [[ -e "$f" ]] || continue
    base="$(basename "$f")"
    [[ -e "$scratch/.claude/agents/$base" ]] || cp "$f" "$scratch/.claude/agents/$base"
  done
  # review-pr-*.md (a glob, like the agents above) is every lens's dispatch
  # command (ADR 0023): adding a lens needs no update here. The non-lens
  # commands (review-pr.md itself, and the reply/edit/judge pipeline stages)
  # aren't name-prefixed the same way, so they stay an explicit list.
  for f in "$repo_root/.claude/commands/review-pr-"*.md; do
    [[ -e "$f" ]] || continue
    base="$(basename "$f")"
    [[ -e "$scratch/.claude/commands/$base" ]] || cp "$f" "$scratch/.claude/commands/$base"
  done
  for f in "$repo_root/.claude/commands/review-pr.md" \
    "$repo_root/.claude/commands/edit-review.md" \
    "$repo_root/.claude/commands/reply-pr.md" \
    "$repo_root/.claude/commands/judge-fix.md"; do
    [[ -e "$f" ]] || continue
    base="$(basename "$f")"
    [[ -e "$scratch/.claude/commands/$base" ]] || cp "$f" "$scratch/.claude/commands/$base"
  done
}

# fetch_open_review_threads <owner> <repo> <pr-number>
# Emits a JSON array of the PR's review threads shaped for resolve_threads.py
# and findings_index.py:
#   [{thread_id, is_resolved, root_author, root_body, root_comment_id,
#     root_comment_url, path, original_line, original_start_line,
#     head_line, head_start_line, has_resolution_stamp}]
# `root_comment_url` is the root comment's web URL, the per-entry link target for
# the Status comment's findings index (ADR 0020); resolve_threads.py ignores it.
# The root (oldest) comment is the Finding; resolve_threads.py filters to the
# open, daemon-owned ones. Each thread carries two coordinate pairs for two jobs.
# `original_line`/`original_start_line` (GraphQL `originalLine`/`originalStartLine`)
# is the creation-side (OLD) coordinate: it drives candidate *selection*, matched
# against the OLD side of the increment diff, and survives `outdated` (GitHub nulls
# `line` exactly when a thread goes outdated, the case commit-driven resolution must
# catch, #125, ADR 0017). `head_line`/`head_start_line` (`line`/`startLine`) is
# GitHub's HEAD-remapped coordinate: it anchors the resolution stamp's HEAD blob
# link, so the link lands on the code at HEAD rather than the creation-side line a
# fix has since shifted. It is null when the thread is outdated (no HEAD mapping
# exists), in which case the stamp drops its anchor. `has_resolution_stamp` is true when the root comment
# carries the stamp's hidden sentinel (ADR 0019), so an already-stamped thread
# skips re-judgment and routes to resolve-only retry (ADR 0017 §4); the literal
# mirrors resolve_threads (RESOLUTION_SENTINEL), pinned by test_resolve_threads.
# `root_comment_id` is the root comment's node id, the target of the in-place stamp
# edit. Fetches comments(first:1): the stamp lives on the root comment (not a later
# reply), so the rest of the thread need not be read. Best-effort: prints `[]` and
# returns non-zero on query failure, so the caller degrades to "no candidates".
fetch_open_review_threads() {
  local owner="$1" repo="$2" pr="$3"
  local resp
  # $owner/$repo/$pr are GraphQL variables, not shell vars; keep them literal.
  # shellcheck disable=SC2016
  if ! resp="$(gh api graphql \
    -f query='query($owner:String!,$repo:String!,$pr:Int!){
      repository(owner:$owner,name:$repo){
        pullRequest(number:$pr){
          reviewThreads(first:100){
            nodes{
              id isResolved path originalLine originalStartLine line startLine
              comments(first:1){ nodes{ id url author{ login } body } }
            }
          }
        }
      }
    }' -f owner="$owner" -f repo="$repo" -F pr="$pr" 2>/dev/null)"; then
    printf '[]'
    return 1
  fi
  jq -c '[.data.repository.pullRequest.reviewThreads.nodes[]
    | {
        thread_id: .id,
        is_resolved: .isResolved,
        root_author: (.comments.nodes[0].author.login // null),
        root_body: (.comments.nodes[0].body // ""),
        root_comment_id: (.comments.nodes[0].id // null),
        root_comment_url: (.comments.nodes[0].url // null),
        path: .path,
        original_line: .originalLine,
        original_start_line: .originalStartLine,
        head_line: .line,
        head_start_line: .startLine,
        has_resolution_stamp: ((.comments.nodes[0].body // "") | contains("<!-- pr-review-agent:resolved -->"))
      }]' <<<"$resp"
}

# derive_project_identity <repo-root>
# Sets PROJECT_URL/PROJECT_NAME from `git remote get-url origin`. Returns
# non-zero if the origin is missing or not a parseable github.com URL.
# shellcheck disable=SC2034  # PROJECT_URL/NAME consumed by callers
derive_project_identity() {
  local repo_root="$1"
  local remote_url derived_owner derived_repo
  remote_url="$(git -C "$repo_root" remote get-url origin 2>/dev/null)" || remote_url=""
  # Constrain repo to no-slash + tolerate trailing slash; strip `.git` shell-side
  # (bash ERE lacks lazy `+?` so `(\.git)?` doesn't compose with greedy capture).
  if [[ "$remote_url" =~ github\.com[:/]([^/]+)/([^/]+)/?$ ]]; then
    derived_owner="${BASH_REMATCH[1]}"
    derived_repo="${BASH_REMATCH[2]%.git}"
  fi
  if [[ -z "${derived_owner:-}" || -z "${derived_repo:-}" ]]; then
    log_err "could not derive project identity — \`git -C $repo_root remote get-url origin\` did not return a parseable github.com URL"
    return 1
  fi
  PROJECT_URL="https://github.com/${derived_owner}/${derived_repo}"
  PROJECT_NAME="$derived_repo"
}

# Provenance tag on every posted artifact that is not a Review body — the
# Inline comment (create-review.sh), the Status comment (below), and the reply
# (create_reply.py's own MARKER). Single bash source: create-review.sh sources lib.sh
# and reads this. Answers "who wrote this", never draft-status (ADR 0010 §1).
# ADR 0010 §3 is the documentary source of truth: this constant and
# create_reply.py's MARKER each hard-code the string — a runtime shared constant
# across the bash/Python boundary was rejected there as costlier than a drift
# test — and test_provenance_tag.py pins the two definitions identical.
# shellcheck disable=SC2034  # also consumed by create-review.sh after sourcing
PROVENANCE_TAG='🤖 _pr-review-agent_'

# Marker identifying the agent's edit-in-place review-status comment (#60).
# find_status_comment keys on it to reuse the one comment across ticks rather
# than post a second. Distinct from the sha sentinel, which lives in the Review
# body and drives dedup; this marker never does.
STATUS_COMMENT_MARKER='<!-- pr-review-agent:status -->'

# status_sha_link <repo-url> <head-oid>
# Renders the head SHA as a short, backtick-wrapped markdown link to its commit
# page on the HEAD repo (the same repo the finding blob links target, so fork
# PRs resolve correctly). Display is the 12-char short SHA; the href uses the
# full SHA so GitHub resolves it unambiguously.
status_sha_link() {
  local repo_url="$1" head_oid="$2"
  # printf format is literal markdown; the values fill the %s (not shell expansion).
  # shellcheck disable=SC2016
  printf '[`%s`](%s/commit/%s)' "${head_oid:0:12}" "$repo_url" "$head_oid"
}

# status_scope_link <repo-url> <last-sha> <head-oid>
# Renders the diff scope: a backtick-wrapped, linked compare range when a prior
# review SHA is known (`<last>..<head>` display, /compare/<last>...<head> href —
# GitHub compare takes THREE dots between full SHAs), or the literal, unlinked
# `full PR` on a first review. Targets the HEAD repo for fork correctness.
status_scope_link() {
  local repo_url="$1" last_sha="$2" head_oid="$3"
  if [[ -z "$last_sha" ]]; then
    printf 'full PR'
  else
    # printf format is literal markdown; the values fill the %s (not shell expansion).
    # shellcheck disable=SC2016
    printf '[`%s..%s`](%s/compare/%s...%s)' \
      "${last_sha:0:12}" "${head_oid:0:12}" "$repo_url" "$last_sha" "$head_oid"
  fi
}

# render_status_comment <head-line> <scope-label> <file-count> <files> [index-block] [trail-block] [sentinel-sha]
# Assembles the status-comment body (#60): header, the optional findings index
# (ADR 0020), the diff scope (commit range + file list, folded in <details> so a
# wide PR stays compact), the optional reviewed-SHAs trail (ADR 0021), and the
# marker find_status_comment keys on. The index is a pointer view (links + state),
# never finding bodies, so it duplicates nothing in the Review object. Omit the
# index arg for the pre-review "Reviewing…" render; the trail (prior rows only at
# that point) still passes through.
#
# sentinel-sha (ADR 0024): embeds discover_sentinel_sha's marker so a completed
# review is discoverable even when it found nothing and create-review.sh's own
# sentinel (ADR 0006) never got (re-)embedded (ADR 0020 skips the review object
# entirely on zero new findings). Pass it ONLY from the terminal "✅ Reviewed"
# render, after the review has actually completed: the pre-review "Reviewing…"
# and the failure renders must never carry it, or a crash/timeout mid-review
# would leave a sentinel claiming a review that never finished, and the next
# tick would wrongly skip re-trying it.
render_status_comment() {
  local head_line="$1" scope_label="$2" file_count="$3" files="$4" index_block="${5:-}" trail_block="${6:-}"
  local sentinel_sha="${7:-}"
  local noun="files"
  [[ "$file_count" == "1" ]] && noun="file"
  local bullets
  # sed script is literal (drop blank lines, wrap each path in `- ` … ``); the
  # `$` is sed's end-of-line, not a shell expansion.
  # shellcheck disable=SC2016
  bullets="$(printf '%s\n' "$files" | sed '/^[[:space:]]*$/d; s/^/- `/; s/$/`/')"
  local body="$head_line"$'\n\n'"_Scope: ${scope_label}_"
  [[ -n "$index_block" ]] && body+=$'\n\n'"$index_block"
  body+=$'\n\n'"<details><summary>${file_count} ${noun}</summary>"$'\n\n'"${bullets}"$'\n\n</details>'
  # Trail sits below the file list and above provenance: scope and index stay the
  # eye's first stop (current state), the trail reads as an appendix (history).
  [[ -n "$trail_block" ]] && body+=$'\n\n'"$trail_block"
  # Visible Provenance tag (ADR 0010), then the hidden sentinel and Status
  # markers last (both HTML comments, neither meant for the reader's eye).
  body+=$'\n\n'"${PROVENANCE_TAG}"
  [[ -n "$sentinel_sha" ]] && body+=$'\n\n'"<!-- pr-review-agent:sha:${sentinel_sha} -->"
  body+=$'\n\n'"${STATUS_COMMENT_MARKER}"
  printf '%s\n' "$body"
}

# status_failure_reason <category>
# Maps a log_failure category slug (ADR 0005's failure table) to a short,
# author-facing sentence shown as a blockquote under the failed status head-line
# (#180), mirroring where a clean review's verdict sits. Fixed UI chrome authored
# here, not agent prose, so it skips the voice.py gate; keep each sentence 두괄식
# and em-dash-free by hand. Returns empty for an unmapped slug so the caller drops
# the blockquote rather than surface a slug the author can't act on.
status_failure_reason() {
  local category="$1"
  case "$category" in
    review-timeout | edit-timeout | *-review-timeout)
      # Every lens (ADR 0023) reads as "the review" to the author; which
      # internal generator stalled is a detail the pipeline owns, not the
      # reader. A glob, not a literal enumeration, so a future lens's
      # <label>-review-timeout category (derived programmatically in
      # daemon/review-pr.sh's dispatch loop from LENS_LABELS) is recognized
      # automatically, with no update needed here.
      printf 'The review agent timed out.'
      ;;
    pending-conflict)
      printf 'An earlier review is still pending on this PR.'
      ;;
    session-limit)
      # The one failure whose cause the author can place: an external quota, not
      # a defect in this PR or in the pipeline, with the retry already scheduled.
      printf 'The review agent ran out of its usage quota, and a retry is scheduled for after the quota resets.'
      ;;
    diff-fetch-timeout)
      printf 'Fetching the PR diff timed out.'
      ;;
    *)
      # Internal agent/pipeline hiccups (empty output, malformed payload, style
      # gate, post error, unknown): the author can't act on them and the next tick
      # retries, so the "will retry next cycle" head-line already says enough.
      # Empty drops the blockquote rather than surface an internal slug.
      printf ''
      ;;
  esac
}

# diff_paths <unified-diff-file>
# Prints the `b/` path of each `diff --git` header, one per line — the file
# list for the status-comment scope.
diff_paths() {
  local diff_file="$1"
  [[ -r "$diff_file" ]] || return 0
  sed -n 's|^diff --git a/.* b/||p' "$diff_file"
}

# find_status_comment <owner> <repo> <pr-number> <operator>
# Prints the id of the operator's status comment (the one carrying
# STATUS_COMMENT_MARKER) so a re-review edits it rather than posting a second
# (#60). Best-effort: returns 0 even on gh failure, falling back to a fresh
# post. `last` wins if more than one slipped through.
find_status_comment() {
  local owner="$1" repo="$2" pr="$3" operator="$4"
  [[ -n "$operator" ]] || return 0
  gh api "repos/${owner}/${repo}/issues/${pr}/comments" --paginate \
    --jq ".[] | select(.user.login == \"${operator}\") | select(.body | contains(\"${STATUS_COMMENT_MARKER}\")) | .id" \
    2>/dev/null | tail -1 || true
}

# post_status_comment <owner> <repo> <pr-number> <body>
# Posts a new issue comment and prints its id. Best-effort: prints nothing and
# returns 0 on failure, so a missing status comment never aborts the review.
post_status_comment() {
  local owner="$1" repo="$2" pr="$3" body="$4"
  gh api "repos/${owner}/${repo}/issues/${pr}/comments" \
    -f body="$body" --jq '.id' 2>/dev/null || true
}

# status_comment_body <owner> <repo> <comment-id>
# Prints an issue comment's body by id, for recovering the reviewed-SHAs trail
# (ADR 0021) before the in-place edit overwrites it. Best-effort: prints nothing
# and returns 0 on an empty id or gh failure, so a fetch miss degrades to a trail
# that restarts rather than aborting the review.
status_comment_body() {
  local owner="$1" repo="$2" comment_id="$3"
  [[ -n "$comment_id" ]] || return 0
  gh api "repos/${owner}/${repo}/issues/comments/${comment_id}" --jq '.body' 2>/dev/null || true
}

# edit_status_comment <owner> <repo> <comment-id> <body>
# Edits an issue comment in place; a no-op on an empty id. This is the most
# user-visible write in the whole pipeline (the reviewed/failed state an
# operator actually reads), so it gets a few quick retries for a transient
# network blip (dogfood-observed: a laptop sleep/resume cycle left `gh api`
# failing right at this call) before giving up. Still best-effort overall —
# a review that already landed must never be aborted over a cosmetic status
# edit, so this always returns 0 — but an exhausted retry is logged, since a
# silent failure here used to leave a PR's status comment stuck on a stale
# "Reviewing" state indefinitely (the daemon already marks that SHA reviewed,
# so nothing else ever retries this specific edit) with no trace in
# .daemon.log to explain it.
edit_status_comment() {
  local owner="$1" repo="$2" comment_id="$3" body="$4"
  local attempt sleep_secs="${STATUS_EDIT_RETRY_SLEEP_SECONDS:-2}"
  [[ -n "$comment_id" ]] || return 0
  for attempt in 1 2 3; do
    gh api -X PATCH "repos/${owner}/${repo}/issues/comments/${comment_id}" \
      -f body="$body" >/dev/null 2>&1 && return 0
    [[ "$attempt" -lt 3 ]] && sleep "$sleep_secs"
  done
  log_info "status comment edit failed after 3 attempts (comment ${comment_id})"
}
