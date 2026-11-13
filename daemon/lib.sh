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
# merge-skip / finding-skip / confidence-gate from merge_findings.py (#196),
# voice-warning and editor-drop from apply_edits.py (a cosmetic style miss
# downgraded to warn-and-post, and the indices the editor dropped, #259). Without
# this a lens silently dropped to four (#196), or a missed voice rule or an editor
# drop left no signal before cleanup() removed the scratch. Lives in lib.sh, not
# inline, so test_degradation_warnings.py can source it (ADR 0026).
log_degradation_warnings() {
  local stderr_file="$1" line
  [[ -r "$stderr_file" ]] || return 0
  while IFS= read -r line; do
    log_info "$line"
  done < <(grep -E '^(merge-skip|finding-skip|confidence-gate|voice-warning|editor-drop)' "$stderr_file" || true)
}

# Failure categories: the one home for ADR 0005's failure table (#141). A
# category is the machine-readable slug on a failure. log_failure prints it in
# the `failure: <category> …` line; a pipeline subprocess instead prints
# `category=<slug>` on its FIRST stderr line, which extract_category recovers and
# hands back to log_failure. Keep this set in step with ADR 0005; a slug the
# daemon doesn't recognize defaults to the loud system-failure path.
#
# Emitted directly here in bash, one slug each (the FAIL_* constants below):
#   repo-unreachable    target repo not accessible
#   diff-fetch-failed   gh pr diff / compare exited non-zero
#   diff-fetch-timeout  gh pr diff exceeded its cap
#   edit-timeout        editor agent exceeded its cap
#   edit-empty          editor produced no output
#   reply-timeout       reply agent exceeded its cap
#   empty-stdout        reply agent produced no output
#   post-failed         posting the review or reply failed
#   unknown             unclassified; the extractor's fallback
# Built dynamically as `<step>-timeout` by run_with_pr_timeout (e.g.
# review-timeout) from the step label it wraps.
# Recovered from a subprocess's `category=` line, never authored via log_failure
# here (a slug may appear under more than one stage):
#   create-review.sh   post-failed
#   create_reply.py    no-fence, parse-error, schema-invalid, style-violation
#   apply_edits.py     edit-empty, edit-no-fence, edit-parse-error, edit-schema-invalid, edit-coverage, edit-fidelity
#   merge_findings.py  empty-stdout, all-lenses-failed, session-limit
#   load_config.py     config-parse-error, config-invalid, config-not-found
# extract_json.py is the shared parse/gate library, not a routed subprocess: its
# ExtractError slugs (empty-stdout, no-fence, parse-error, schema-invalid,
# style-violation, cap-violation) are caught by merge_findings, which logs a
# single failed lens as a merge-skip warning and rolls a total failure up to
# all-lenses-failed (or session-limit).
#
# shellcheck disable=SC2034  # each is used by a sibling script that re-sources this file
readonly FAIL_REPO_UNREACHABLE="repo-unreachable"
# shellcheck disable=SC2034
readonly FAIL_DIFF_FETCH_FAILED="diff-fetch-failed"
# shellcheck disable=SC2034
readonly FAIL_DIFF_FETCH_TIMEOUT="diff-fetch-timeout"
# shellcheck disable=SC2034
readonly FAIL_EDIT_TIMEOUT="edit-timeout"
# shellcheck disable=SC2034
readonly FAIL_EDIT_EMPTY="edit-empty"
# shellcheck disable=SC2034
readonly FAIL_REPLY_TIMEOUT="reply-timeout"
# shellcheck disable=SC2034
readonly FAIL_EMPTY_STDOUT="empty-stdout"
# shellcheck disable=SC2034
readonly FAIL_POST_FAILED="post-failed"
# shellcheck disable=SC2034
readonly FAIL_UNKNOWN="unknown"

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

# Exit status run_with_timeout returns on timeout, and the status the shell
# reports for a SIGALRM kill (128 + 14). Callers branch on it to route to the
# ADR 0005 failure path instead of parsing the truncated output.
# shellcheck disable=SC2034  # consumed by reply-pr.sh / review-pr.sh
readonly TIMEOUT_EXIT=142

# How often the watchdog re-reads the clock, and how long it lets a terminated
# job flush before escalating to SIGKILL. Both bound only the overrun path, never
# a command that finishes under its cap. Overridable for tests.
readonly TIMEOUT_POLL_INTERVAL="${TIMEOUT_POLL_INTERVAL:-5}"
readonly TIMEOUT_KILL_GRACE="${TIMEOUT_KILL_GRACE:-5}"

# process_tree <pid>
# Prints every live process in <pid>'s tree, deepest first, one per line, and
# nothing at all once the tree is gone. Deepest first so a parent cannot spawn a
# replacement in the gap between its child dying and its own signal arriving.
# (Shallowest first is the right order only against a supervisor that restarts
# dead children, which nothing the daemon runs is.) Emptiness is load-bearing:
# the escalation loop reads it as "the tree is gone" and stops.
process_tree() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    process_tree "$child"
  done
  kill -0 "$pid" 2>/dev/null && printf '%s\n' "$pid"
  # An empty tree is the normal terminal case, not a failure: the callers run
  # under `set -e` and assign this in a command substitution.
  return 0
}

# live_pids <pid...>
# Prints the arguments that are still running, deduplicated. Same empty-is-not-
# failure contract as process_tree.
live_pids() {
  local pid
  for pid in "$@"; do
    kill -0 "$pid" 2>/dev/null && printf '%s\n' "$pid"
  done | sort -u
  return 0
}

# foreign_groups <pid...>
# Prints the process group of each given process, except the caller's own,
# which holds the daemon itself. A group reaches a command that double-forked
# out of the tree, because its orphan keeps the group it was started in, as long
# as another member of that group is still in the tree.
foreign_groups() {
  local own pid pgid
  own="$(ps -o pgid= -p $$ | tr -d ' ')"
  for pid in "$@"; do
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')" || continue
    [[ -n "$pgid" && "$pgid" -gt 1 && "$pgid" != "$own" ]] && printf '%s\n' "$pgid"
  done | sort -u
  return 0
}

# kill_with_groups <pid...>
# Kills the given processes and their foreign_groups. Freezing everything first
# stops a process forking a replacement between the group kills and the pid
# kills.
kill_with_groups() {
  local pgid groups
  kill -STOP "$@" 2>/dev/null || true
  groups="$(foreign_groups "$@")"
  for pgid in $groups; do
    kill -KILL -- "-$pgid" 2>/dev/null || true
  done
  kill -KILL "$@" 2>/dev/null || true
}

# run_with_timeout <seconds> <command...>
# Caps a command's wall-clock runtime. Returns the command's own exit status, or
# $TIMEOUT_EXIT on timeout.
#
# The cap is a `date +%s` deadline, not perl's `alarm`. alarm arms ITIMER_REAL,
# which counts kernel-timer time and so stops advancing while the machine is
# suspended; epoch time does not, because it is re-read from the hardware clock
# on wake. A review that spanned a laptop sleep outlived a 600s cap by well over
# an hour without the alarm ever firing, the same suspend-shaped failure
# arm_git_stall_timeout already guards git against.
#
# Only a watchdog child polls; the parent blocks in `wait`, so a command that
# finishes early returns immediately. Polling in the parent instead would round
# every call up to the interval, and most calls here are sub-second `gh api`.
# That also lets the interval stay coarse: it bounds how late an overrun is
# noticed, which for a backstop measured in minutes costs nothing. The watchdog's
# own output goes to /dev/null: it outlives a command that finished early, and
# would otherwise hold the write end of the caller's pipe open for the rest of
# the interval, which is the latency the parent's `wait` exists to avoid.
#
# Termination walks descendants rather than putting the job in its own process
# group. A group is the tidier kill, but `set -m` also moves the job out of the
# terminal's foreground group, which stops Ctrl-C reaching a running review, and
# gives a nested call its own group that the outer call's group kill cannot
# reach. This function nests: run_with_pr_timeout wraps review-pr.sh, which calls
# it again per lens. The walk is not atomic, so a process forking mid-walk can
# escape; for a backstop that is the better trade than a kill that composes badly.
#
# The escalation also signals the foreign_groups of the tree, so an outer cap
# reaches what a nested call started in groups of its own even after killing the
# nested watchdog that would have cleaned them up. TIMEOUT_KILL_GROUPS=1
# replaces the escalation with kill_with_groups, for a command that starts its
# children in groups of their own and needs no flush on the way out. Neither
# reaches a process that left the tree when no member of its group is left in
# it, which includes one that called setsid.
run_with_timeout() {
  local secs="$1"
  shift
  local deadline=$(($(date +%s) + secs))
  # Empty means the command ended on its own; the watchdog writes to it before
  # signalling. A flag file, not the exit status, because a command may die of
  # its own signal and must not be misreported as having hit the cap.
  local fired
  fired="$(mktemp -t pr-review-timeout.XXXXXX)"

  # `0<&0` keeps the caller's stdin. Bash points an async command at /dev/null
  # unless it is given an explicit redirect, and callers redirect into the
  # function rather than the command: `run_with_timeout N claude … <prompt` binds
  # the file to this function, and every lens then read an empty prompt and
  # produced nothing. The perl-exec helper this replaced inherited stdin, so the
  # redirect worked by accident and no call site spells it out.
  "$@" 0<&0 &
  local pid=$!

  (
    # Sleep the lesser of the interval and the time left, so the interval bounds
    # how often the clock is re-read (what catches a suspend) without also
    # coarsening when the cap fires.
    local now remaining
    while :; do
      now="$(date +%s)"
      [[ "$now" -ge "$deadline" ]] && break
      kill -0 "$pid" 2>/dev/null || exit 0
      remaining=$((deadline - now))
      [[ "$remaining" -gt "$TIMEOUT_POLL_INTERVAL" ]] && remaining="$TIMEOUT_POLL_INTERVAL"
      sleep "$remaining"
    done
    kill -0 "$pid" 2>/dev/null || exit 0
    printf 1 >"$fired"
    # TERM first so claude -p can flush its output and cost sidecar, then KILL,
    # because the case this guard exists for is a process wedged past a suspend
    # that may never act on a TERM. Every kill tolerates failure: a process can
    # exit on its own between the walk and the signal.
    # Snapshot the tree before signalling. A child re-parents to init the instant
    # its parent dies, so a walk from "$pid" after the TERM cannot see the very
    # processes the escalation is for: the ones that outlived a parent that took
    # the TERM. This is what a process group gets for free and the walk does not.
    local victims survivors groups pgid
    victims="$(process_tree "$pid")"
    if [[ "${TIMEOUT_KILL_GROUPS:-0}" -eq 1 ]]; then
      # shellcheck disable=SC2086  # word splitting is the point: one pid per arg
      kill_with_groups $victims
      exit 0
    fi
    # shellcheck disable=SC2086
    groups="$(foreign_groups $victims)"
    # shellcheck disable=SC2086
    kill -TERM $victims 2>/dev/null || true
    for pgid in $groups; do
      kill -TERM -- "-$pgid" 2>/dev/null || true
    done
    local grace=$(($(date +%s) + TIMEOUT_KILL_GRACE))
    while :; do
      # The snapshot catches the re-parented; a fresh walk catches anything
      # spawned since. Neither alone is enough.
      # shellcheck disable=SC2046,SC2086
      survivors="$(live_pids $victims $(process_tree "$pid"))"
      [[ -z "$survivors" ]] && break
      if [[ "$(date +%s)" -ge "$grace" ]]; then
        # shellcheck disable=SC2086
        kill -KILL $survivors 2>/dev/null || true
        break
      fi
      sleep 1
      victims="$survivors"
    done
    # An orphan in a group is not among the survivors, so the walk ending says
    # nothing about it.
    for pgid in $groups; do
      kill -KILL -- "-$pgid" 2>/dev/null || true
    done
  ) >/dev/null 2>&1 &
  local watchdog=$!

  local rc=0
  # stderr silenced because bash announces a signal-killed job ("Terminated: 15")
  # here, which on every cap would land in the daemon log next to the structured
  # failure line that already says the same thing.
  wait "$pid" 2>/dev/null || rc=$?
  if [[ -s "$fired" ]]; then
    # The cap fired, so let the escalation finish rather than cutting it short.
    # `wait "$pid"` returns as soon as the command itself dies, which under a
    # TERM it honours is long before the grace window that exists for the
    # children it left behind. Returning here would hand the caller a timeout
    # while the tree it names is still running. The watchdog writes the flag
    # before it signals, so seeing it set means the escalation is already under
    # way and bounded by TIMEOUT_KILL_GRACE.
    wait "$watchdog" 2>/dev/null || true
  else
    kill "$watchdog" 2>/dev/null || true
    wait "$watchdog" 2>/dev/null || true
  fi

  [[ -s "$fired" ]] && rc="$TIMEOUT_EXIT"
  rm -f "$fired"
  return "$rc"
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

# resolve_review_model <dotenv-path> [harness]
# Resolve the reasoning model every daemon agent call pins, printing it.
# The default lives here alone: it reaches the agents as the --model argument
# run_agent expands, never as an environment variable, so a script that resolved it
# separately would be free to drift onto a different model. Unlike the dials
# resolve_tunable serves, an empty value is not a usable default here (`--model
# ""` is a broken flag), so this one defaults in the shell rather than downstream.
# Under the codex harness an unset or Claude model fails instead, at startup,
# because Codex rejects a Claude model ID and every review would fail on it.
resolve_review_model() {
  local model
  model="$(resolve_tunable REVIEW_MODEL "$1")"
  if [[ "${2:-}" == codex && (-z "$model" || "$model" == claude-*) ]]; then
    log_err "REVIEW_HARNESS=codex needs REVIEW_MODEL (env or .env) set to a model the codex CLI accepts; '${model:-the default}' is a Claude model"
    return 1
  fi
  printf '%s' "${model:-claude-opus-5-5}"
}

# resolve_review_effort <dotenv-path>
# Resolve the effort level every daemon agent call pins beside the model, printing
# it, because the API default effort varies by model and a model bump would
# otherwise change review depth unannounced. claude -p only warns on an unknown
# level and runs at the model default, so an invalid value falls back here.
resolve_review_effort() {
  local effort default=high
  effort="$(resolve_tunable REVIEW_EFFORT "$1")"
  if [[ -z "$effort" ]]; then
    effort="$default"
  elif [[ " $REVIEW_EFFORT_LEVELS " != *" $effort "* ]]; then
    log_info "ignoring invalid REVIEW_EFFORT='${effort}', using default ${default}"
    effort="$default"
  fi
  printf '%s' "$effort"
}

# The levels resolve_review_effort accepts, space-separated; bin/setup-model.sh
# offers the same list.
REVIEW_EFFORT_LEVELS="low medium high xhigh max"

# resolve_review_harness <dotenv-path>
# Resolve which agent CLI run_agent drives, printing it: claude (the default) or
# codex. An unknown value fails instead of falling back, because reviewing on a
# CLI the operator did not pick is worse than not reviewing.
resolve_review_harness() {
  local harness
  harness="$(resolve_tunable REVIEW_HARNESS "$1")"
  case "${harness:-claude}" in
    claude | codex) printf '%s' "${harness:-claude}" ;;
    *)
      log_err "REVIEW_HARNESS='${harness}' is not claude or codex"
      return 1
      ;;
  esac
}

# set_dotenv_keys <dotenv-path> KEY=VALUE...
# Sets each KEY on the first `KEY=` line, the one resolve_tunable reads, or
# appends it, leaving every other byte as it was. A missing .env starts from the
# template. The new file replaces the old in one rename beside it, so a reader
# never sees half a file and a symlinked .env is written through. A value outside
# a plain model-name alphabet is refused, because it would land unquoted in a
# file other scripts parse with grep.
set_dotenv_keys() {
  local dotenv="$1" pair real src tmp
  local value_re='^[A-Z_][A-Z0-9_]*=[]A-Za-z0-9._:/@[-]+$'
  shift
  for pair in "$@"; do
    if [[ ! "$pair" =~ $value_re ]]; then
      log_err "not writing ${pair%%=*}: the value is empty or has a character other than letters, digits, and . _ : / @ [ ] -"
      return 1
    fi
  done
  real="$(physical_path "$dotenv")" || return 1
  src="$real"
  [[ -e "$src" ]] || src="$(dirname "${BASH_SOURCE[0]}")/../templates/.env.example"
  if ! tmp="$(mktemp "$(dirname "$real")/.env.XXXXXX")"; then
    log_err "could not write ${dotenv}; it is unchanged"
    return 1
  fi
  if python3 -c '
import re, sys
src, out, *pairs = sys.argv[1:]
data = open(src, "rb").read()
for pair in pairs:
    line = pair.encode()
    key = line.split(b"=", 1)[0]
    found = re.search(rb"^" + re.escape(key) + rb"=[^\r\n]*", data, re.M)
    if found:
        data = data[: found.start()] + line + data[found.end() :]
    else:
        data += (b"" if data.endswith(b"\n") or not data else b"\n") + line + b"\n"
open(out, "wb").write(data)
' "$src" "$tmp" "$@" && chmod 600 "$tmp" && mv -f "$tmp" "$real"; then
    return 0
  fi
  rm -f "$tmp"
  log_err "could not write ${dotenv}; it is unchanged"
  return 1
}

# offer_model_setup <dotenv-path>
# Offers bin/setup-model.sh at an interactive start when neither the environment
# nor .env names a REVIEW_MODEL, and fails only when that setup fails. Without a
# terminal it logs a hint and returns 0, because launchd restarts the job on a
# non-zero exit.
offer_model_setup() {
  local answer
  [[ -z "$(resolve_tunable REVIEW_MODEL "$1")" ]] || return 0
  if [[ ! -t 0 ]]; then
    log_info "no review model chosen, so reviews run on the defaults; run bash bin/setup-model.sh in a terminal to choose one"
    return 0
  fi
  printf 'No review model chosen. Set it up now? [Y/n] ' >&2
  IFS= read -r answer || answer=n
  case "$answer" in
    "" | [Yy]*) bash "$(dirname "${BASH_SOURCE[0]}")/../bin/setup-model.sh" "$1" ;;
    *) log_info "continuing on the defaults; run bash bin/setup-model.sh to choose a model later" ;;
  esac
}

# agent_fields <agent-file>
# Prints an agent file's name, description, and tools, one per line, or prints
# `<file>:<line>: <problem>` and fails, because a definition read differently
# from how its author meant it still runs, as a different agent. The one shape
# read: LF line endings; a frontmatter block opening on line 1 and closed by the
# next `---`; inside it exactly `name`, `description`, and `tools` as plain
# `key: value` lines (one matching pair of surrounding quotes is stripped),
# `tools` a comma-separated list; then a non-empty body.
agent_fields() {
  awk '
    function fail(msg) {
      printf "%s:%d: %s\n", FILENAME, FNR, msg
      failed = 1
      exit 1
    }
    /\r$/ { fail("CRLF line ending; save the file with LF line endings") }
    state == 2 { if ($0 ~ /[^ \t]/) body = 1; next }
    FNR == 1 {
      if ($0 != "---") fail("expected --- opening the frontmatter on line 1")
      state = 1
      next
    }
    $0 == "---" {
      if (!("name" in val)) fail("frontmatter has no name line")
      if (!("description" in val)) fail("frontmatter has no description line")
      if (!("tools" in val)) fail("frontmatter has no tools line")
      state = 2
      next
    }
    {
      if ($0 !~ /^[a-z]+:( |$)/) fail("expected a plain key: value line")
      key = $0
      sub(/:.*/, "", key)
      v = substr($0, length(key) + 2)
      sub(/^[ \t]+/, "", v)
      sub(/[ \t]+$/, "", v)
      if (key != "name" && key != "description" && key != "tools") fail("unsupported key " key)
      if (key in val) fail("duplicate key " key)
      q = substr(v, 1, 1)
      if ((q == "\"" || q == "\047") && length(v) >= 2 && substr(v, length(v)) == q) {
        v = substr(v, 2, length(v) - 2)
        if (index(v, q) || (q == "\"" && index(v, "\\"))) fail("unsupported quoting in " key)
      } else if (v ~ /^([>|"\047#&*!%@`[{]|- )/ || v ~ /[ \t]#/) {
        fail("unsupported YAML in " key "; write a plain value on one line")
      }
      if (v == "") fail("empty " key)
      if (key == "tools") {
        n = split(v, tool, ",")
        for (i = 1; i <= n; i++) {
          t = tool[i]
          gsub(/^[ \t]+|[ \t]+$/, "", t)
          if (t !~ /^[A-Za-z][A-Za-z0-9_]*(\([^()]*\))?$/) fail("tools entry \"" t "\" is not a tool name; list them as Read, Grep")
        }
      }
      val[key] = v
    }
    END {
      if (failed) exit 1
      if (state == 0) fail("empty file")
      if (state == 1) fail("frontmatter is never closed by ---")
      if (!body) fail("empty body after the frontmatter")
      print val["name"]
      print val["description"]
      print val["tools"]
    }
  ' "$1"
}

# agent_body <agent-file>
# Prints an agent file's body verbatim: every line after the frontmatter's
# closing `---`, a later `---` line included.
agent_body() {
  awk 'done { print; next } NR > 1 && $0 == "---" { done = 1 }' "$1"
}

# agent_definition <agent-file>
# Prints one agent file as the `{<name>: {description, prompt, tools}}` entry
# `claude --agents` takes, or logs where the file breaks the shape agent_fields
# reads and fails.
agent_definition() {
  local fields name description tools
  if ! fields="$(agent_fields "$1")"; then
    log_err "agent definition ${fields}"
    return 1
  fi
  {
    IFS= read -r name
    IFS= read -r description
    IFS= read -r tools
  } <<<"$fields"
  jq -n --arg name "$name" --arg description "$description" --arg tools "$tools" \
    --arg prompt "$(agent_body "$1")" \
    '{($name): {description: $description, prompt: $prompt,
      tools: ($tools | split(",") | map(gsub("^\\s+|\\s+$"; "")))}}'
}

# daemon_agents_json
# Prints this checkout's review-agent-*.md definitions as the JSON object
# `claude --agents` takes, keyed by each file's frontmatter name.
daemon_agents_json() {
  local f def defs=()
  for f in "$(dirname "${BASH_SOURCE[0]}")/../.claude/agents/review-agent-"*.md; do
    def="$(agent_definition "$f")" || return 1
    defs+=("$def")
  done
  printf '%s\n' "${defs[@]}" | jq -s add
}

# write_run_file <path> <content>
# Removes whatever is at <path> (a symlink itself, not its target) before
# writing <content> there, because an agent earlier in the same review can leave
# a directory or symlink at a name run_agent reuses.
write_run_file() {
  rm -rf -- "$1"
  if ! printf '%s\n' "$2" 2>/dev/null >"$1"; then
    log_err "run_agent: cannot write ${1}"
    return 1
  fi
}

# The oldest Claude Code that honours what run_agent sends: the --agents file
# form needs v2.1.281, --restricted v2.1.248, and the sandbox's
# network.strictAllowlist v2.1.219, and a CLI that ignores the last waits on a
# network approval nobody can give.
CLAUDE_MIN_VERSION=2.1.281

# require_claude_version
# Fails with an upgrade hint unless `claude --version` is at least
# $CLAUDE_MIN_VERSION.
require_claude_version() {
  local have
  have="$(claude --version 2>/dev/null)" || have=""
  have="${have%% *}"
  if [[ ! "$have" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
    (($(_version_key "$have") < $(_version_key "$CLAUDE_MIN_VERSION"))); then
    log_err "claude ${have:-of unknown version} is older than ${CLAUDE_MIN_VERSION}, which the agent sandbox needs; run 'claude update'"
    return 1
  fi
}

# require_real_codex
# Resolves codex as every agent call does and has it validate the argv every
# call sends, so a PATH holding only a shim, or a codex too old for a flag or
# config key, fails here once instead of failing every call. Rests on codex's
# own validation rather than a version floor, because no release note names
# when each key arrived. Logs the binary chosen when a shim came first on PATH.
require_real_codex() {
  local note
  note="$(python3 "$(dirname "${BASH_SOURCE[0]}")/codex_exec.py" check)" || return 1
  [[ -z "$note" ]] || log_info "$note"
}

_version_key() {
  local major minor patch
  IFS=. read -r major minor patch <<<"$1"
  printf '1%06d%06d%06d' "$major" "$minor" "$patch"
}

# agent_sandbox_settings <agent-tmp>
# Prints the --settings JSON for the Agent sandbox (CONTEXT.md), and is where its
# bounds are stated. A shell command runs sandboxed or not at all, and is never
# auto-allowed. It reads nothing under the home directory, $TMPDIR, the directory
# `mktemp -t` uses, /tmp, the daemon's state directory, or this checkout, nor the
# App private key wherever it lives, except the per-PR clone and <agent-tmp>,
# which allowRead re-opens because the narrower rule wins; it writes nothing in
# the clone and reaches no network. Paths are physical because /tmp and /var are
# symlinks on macOS.
agent_sandbox_settings() {
  local clone agent_tmp tmpdir mktemp_dir slash_tmp state checkout key
  clone="$(pwd -P)" &&
    agent_tmp="$(cd "$1" && pwd -P)" || return 1
  # The daemon's own temp files land where mktemp puts them, which on macOS is
  # the user temp directory even when TMPDIR is unset or names another one.
  if ! mktemp_dir="$(mktemp -u -t pr-review-probe.XXXXXX)"; then
    log_err "run_agent: mktemp cannot name the temp directory to deny"
    return 1
  fi
  tmpdir="$(physical_path "${TMPDIR:-/tmp}")" &&
    mktemp_dir="$(physical_path "$(dirname "$mktemp_dir")")" &&
    slash_tmp="$(physical_path /tmp)" &&
    state="$(physical_path "$(_state_dir)")" &&
    checkout="$(physical_path "$(dirname "${BASH_SOURCE[0]}")/..")" &&
    key="$(physical_path "$APP_KEY_PATH")" || return 1
  jq -cn --arg clone "$clone" --arg agent_tmp "$agent_tmp" --arg tmpdir "$tmpdir" \
    --arg mktemp_dir "$mktemp_dir" --arg slash_tmp "$slash_tmp" --arg state "$state" \
    --arg checkout "$checkout" --arg key "$key" '{sandbox: {
    enabled: true,
    failIfUnavailable: true,
    allowUnsandboxedCommands: false,
    autoAllowBashIfSandboxed: false,
    filesystem: {
      denyRead: (["~/", $tmpdir, $mktemp_dir, $slash_tmp, $state, $checkout] | unique),
      allowRead: [$clone, $agent_tmp],
      denyWrite: [$clone]
    },
    credentials: {files: [{path: $key, mode: "deny"}]},
    network: {strictAllowlist: true}
  }}'
}

# physical_path <path>
# Prints <path> with every symlink resolved, including a final one, and works on
# a path that does not exist yet, which `cd && pwd -P` cannot do.
physical_path() {
  python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

# agent_system_prompt <agent-name> <out>
# Writes the body of this checkout's .claude/agents/<agent-name>.md to <out> as
# a directly-prompted agent's system prompt, or logs why it cannot and fails.
agent_system_prompt() {
  local agent_file
  agent_file="$(dirname "${BASH_SOURCE[0]}")/../.claude/agents/${1}.md"
  if [[ ! -r "$agent_file" ]]; then
    log_err "run_agent: no agent definition '${1}'"
    return 1
  fi
  agent_definition "$agent_file" >/dev/null || return 1
  write_run_file "$2" "$(agent_body "$agent_file")"
}

# run_agent <mode> <timeout-secs> <prompt-file> <raw-out> <label> [agent-name]
# The daemon's one way to run an agent: feeds <prompt-file> on stdin, streams
# labelled activity lines to the terminal, and leaves the agent's final text in
# <raw-out> (empty on timeout), its token count in <raw-out>.tokens, its cost in
# <raw-out>.cost when the CLI reports one (codex does not), and its own stderr
# in <raw-out>.stderr. Drives the CLI $REVIEW_HARNESS names (claude when unset)
# and pins $REVIEW_MODEL and $REVIEW_EFFORT, all three resolved by the caller.
# Returns the agent's exit status, $TIMEOUT_EXIT past the cap, or the
# formatter's status, under the daemon's `set -o pipefail`.
#
# <mode> says what the agent may do:
#   read-only    inspect the clone and answer in its final message, with the body
#                of this checkout's .claude/agents/<agent-name>.md as its system
#                prompt (written to <raw-out>.system.md).
#   orchestrate  spawn the role subagents defined by this checkout's
#                .claude/agents/ (written to <raw-out>.agents.json), each writing
#                its payload file into the clone. Claude only.
#
# Every definition comes from this checkout, never the clone, because the clone
# holds the PR head and its author controls it.
#
# Run it from inside the per-PR clone: the agent's working directory is the
# caller's, and orchestrate's file writes are scoped to it.
run_agent() {
  case "$1" in
    read-only | orchestrate) ;;
    *)
      log_err "run_agent: unknown mode '${1}'"
      return 1
      ;;
  esac
  # The scripts reject an unknown harness at startup, but a caller that skips
  # resolve_review_harness must not silently review on claude either.
  case "${REVIEW_HARNESS:-claude}" in
    claude) run_agent_claude "$@" ;;
    codex) run_agent_codex "$@" ;;
    *)
      log_err "run_agent: unknown harness '${REVIEW_HARNESS}'"
      return 1
      ;;
  esac
}

run_agent_claude() {
  local mode="$1" secs="$2" prompt="$3" raw="$4" label="$5" agent="${6:-}"
  local args=(-p)
  if [[ -n "$agent" ]]; then
    agent_system_prompt "$agent" "$raw.system.md" || return 1
    args+=(--append-system-prompt-file "$raw.system.md")
  fi
  args+=(--model "$REVIEW_MODEL" --effort "$REVIEW_EFFORT")
  case "$mode" in
    read-only) args+=(--tools Read Grep Glob Bash) ;;
    orchestrate)
      local agents_json
      agents_json="$(daemon_agents_json)" || return 1
      write_run_file "$raw.agents.json" "$agents_json" || return 1
      # --tools carries the union of what the role agents need plus Agent: a
      # subagent's effective tool set is the intersection of its frontmatter list
      # and the parent session's --tools, so a tool missing here is silently
      # unavailable to every role no matter what the agent file grants.
      # --permission-mode acceptEdits: non-interactive claude auto-denies the
      # Write permission prompt, so without this every role completes its review
      # and then fails to land its payload file. acceptEdits auto-approves file
      # writes inside the session's working directory, which is the per-PR clone.
      args+=(--tools Agent Read Write Bash Grep Glob WebFetch --permission-mode acceptEdits
        --agents "$raw.agents.json")
      ;;
  esac
  # --restricted ignores every settings file (user, project, and local) and
  # stops CLAUDE.md loading, so the clone's author cannot plant hooks, env, or
  # instructions; it also confines file tools to the per-PR clone. Slash
  # commands are disabled because ADR 0034 measured the slash-command wrapper as
  # pure forwarding overhead; every call is directly prompted (ADR 0038).
  args+=(--restricted --strict-mcp-config --disable-slash-commands
    --output-format stream-json --verbose)
  # Claude Code's own temp files, the Bash tool's output among them, go to a
  # per-run directory rather than the shared /tmp/claude-<uid>, which holds the
  # operator's other sessions; it sits directly under /tmp because Claude Code
  # hands sandboxed commands that shared directory as $TMPDIR when the path is
  # long.
  local agent_tmp settings rc=0 traps
  agent_tmp="$(mktemp -d /tmp/pr-review-claude.XXXXXX)" || return 1
  if ! settings="$(agent_sandbox_settings "$agent_tmp")"; then
    rm -rf "$agent_tmp"
    return 1
  fi
  args+=(--settings "$settings")
  # The call sites run this in a subshell, where the caller's signal traps do
  # not apply, so a TERM or INT would kill it before the rm below and leave
  # behind shell output derived from the PR.
  traps="$(trap -p INT TERM)"
  trap 'rm -rf "$agent_tmp"; exit 130' INT
  trap 'rm -rf "$agent_tmp"; exit 143' TERM
  # claude's own stderr (e.g. its non-interactive workspace-trust notice,
  # expected every run since a fresh scratch clone is never pre-trusted) goes to
  # a sidecar file so the primary log stays legible.
  # The GIT_CONFIG_* variables keep git off its files in the home directory,
  # which the sandbox denies: git exits on an unreadable ~/.gitconfig.
  run_with_timeout "$secs" env CLAUDE_CODE_TMPDIR="$agent_tmp" \
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_COUNT=2 \
    GIT_CONFIG_KEY_0=core.excludesFile GIT_CONFIG_VALUE_0=/dev/null \
    GIT_CONFIG_KEY_1=core.attributesFile GIT_CONFIG_VALUE_1=/dev/null \
    claude "${args[@]}" <"$prompt" 2>"$raw.stderr" |
    python3 "$(dirname "${BASH_SOURCE[0]}")/stream_format.py" --raw-out "$raw" \
      --label "$label" --cost-out "$raw.cost" || rc=$?
  rm -rf "$agent_tmp"
  trap - INT TERM
  eval "$traps"
  return "$rc"
}

# Same contract as run_agent_claude; codex_exec.py builds the argv and says why
# each flag is there. Its sandbox shares the system temp roots between every
# command it runs, so a clone under them is refused rather than exposed.
run_agent_codex() {
  local mode="$1" secs="$2" prompt="$3" raw="$4" label="$5" agent="${6:-}"
  local here workdir arg argv=() pid rc=0 format_rc=0
  # Each output name is cleared as write_run_file clears it, so nothing the clone
  # carries there is written through or read back as output, then <raw> and the
  # events file exist before anything can fail or the formatter follows them.
  rm -rf -- "$raw" "$raw.events" "$raw.stderr" "$raw.tokens" "$raw.cost"
  if ! { : >"$raw" && : >"$raw.events"; } 2>/dev/null; then
    log_err "run_agent: cannot write ${raw}"
    return 1
  fi
  if [[ "$mode" == orchestrate ]]; then
    log_err "run_agent: codex cannot run orchestrate mode because its subagents are disabled, so every model session holds a slot; run each review role as its own read-only agent instead"
    return 1
  fi
  workdir="$(pwd -P)"
  case "$workdir/" in
    /tmp/* | /private/tmp/* | /var/tmp/* | /private/var/tmp/*)
      log_err "run_agent: refusing to run codex in ${workdir}: its sandbox denies the system temp directories to every command, the clone included; set TMPDIR to a private directory so the per-PR clone lands outside them"
      return 1
      ;;
  esac
  if [[ -n "$agent" ]]; then
    agent_system_prompt "$agent" "$raw.system.md" || return 1
  fi
  here="$(dirname "${BASH_SOURCE[0]}")"
  while IFS= read -r -d '' arg; do
    argv+=("$arg")
  done < <(python3 "$here/codex_exec.py" argv --workdir "$workdir" --raw-out "$raw" \
    --model "$REVIEW_MODEL" --effort "$REVIEW_EFFORT" ${agent:+--system "$raw.system.md"})
  # codex_exec.py prints nothing, and says why on stderr, when it cannot build one.
  ((${#argv[@]} > 0)) || return 1
  # Events go to a file the formatter follows, not a pipe: a command that
  # escapes the kill and inherits codex's stdout would hold a pipe open, and the
  # formatter would wait on it past the cap. The group kill reaches what the
  # descendant walk misses, because codex starts each command in its own group.
  TIMEOUT_KILL_GROUPS=1 run_with_timeout "$secs" "${argv[@]}" \
    <"$prompt" >"$raw.events" 2>"$raw.stderr" &
  pid=$!
  python3 "$here/codex_exec.py" format --events "$raw.events" --while-pid "$pid" \
    --label "$label" --tokens-out "$raw.tokens" || format_rc=$?
  wait "$pid" || rc=$?
  # The formatter's status first, as pipefail would report it.
  ((format_rc == 0)) || return "$format_rc"
  return "$rc"
}

# run_role_agents <timeout-secs> {<role> <activity-label> <prompt-file> <raw-out> <agent-name>}...
# Runs each role as its own read-only agent, in parallel, one slot each, for a
# harness whose subagents are disabled. Waits for every role, not just the first to
# fail, because a role still running when this returns races the caller's
# scratch cleanup (ADR 0026). A failed role is tolerated: its raw file stays
# empty and merge_findings.py drops it. Returns $TIMEOUT_EXIT when any role hit
# the cap, else the last failed role's status, else 0. Run it from inside the
# per-PR clone.
run_role_agents() {
  local secs="$1" i rc status=0 pids=() roles=()
  shift
  while (($# >= 5)); do
    (
      slot="$(acquire_claude_slot "$1 role")"
      rc=0
      run_agent read-only "$secs" "$3" "$4" "$2" "$5" || rc=$?
      release_claude_slot "$slot"
      exit "$rc"
    ) &
    pids+=("$!")
    roles+=("$1")
    shift 5
  done
  for ((i = 0; i < ${#pids[@]}; i++)); do
    rc=0
    wait "${pids[$i]}" || rc=$?
    if ((rc == TIMEOUT_EXIT)); then
      log_info "${roles[$i]} role exceeded ${secs}s"
      status="$TIMEOUT_EXIT"
    elif ((rc != 0)); then
      log_info "${roles[$i]} role exited ${rc}"
      ((status == TIMEOUT_EXIT)) || status="$rc"
    fi
  done
  return "$status"
}

# print_role_task <role> <harness> <diff-basename> <intent-basename> <payload-basename>
# The task prompt one review role receives. Under claude the role writes its
# payload to <payload-basename>; codex's sandbox cannot write, so there the
# role's final message is the payload.
print_role_task() {
  printf 'Review this PR per your instructions.\n'
  printf 'The line-numbered diff is at: %s\n' "$3"
  # ADR 0035: the intent role gets the second side of its comparison. The
  # code role has only the diff, which is the quarantine ADR 0038 names.
  if [[ "$1" == "intent" ]]; then
    printf 'What the change says it does is at: %s\n' "$4"
  fi
  # The diff goes by path, not inlined: inlining it and saying "reason over
  # this" made the model treat the context as complete and skip opening the
  # callers, which is the verify step's whole substance (ADR 0022).
  printf 'Read it, then investigate the surrounding code with your tools '
  printf '(open the callers, trace the data flow) to verify each candidate before scoring.\n'
  if [[ "$2" == codex ]]; then
    printf 'Your final message is your payload: end it with the complete ```json fenced payload your instructions require.\n'
  else
    printf 'Write your complete output, the ```json fenced payload your instructions require, to a new file named %s. ' "$5"
    printf 'Your final reply must not restate the payload; one line confirming the write is enough.\n'
  fi
}

# clear_reserved_names <clone>
# Every file the daemon writes into the per-PR clone is named `.pr-review-*` at
# the clone's root, and that prefix is reserved: run this right after the
# checkout so an entry the PR committed there can be neither read back as daemon
# output, written through as a symlink, nor left to fail a write as a directory.
# The match ignores case because a case-insensitive filesystem resolves any
# casing of the prefix to the daemon's own file, and `rm` removes a symlink, not
# its target.
clear_reserved_names() {
  find "$1" -mindepth 1 -maxdepth 1 -iname '.pr-review-*' -exec rm -rf {} +
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
  status="$(run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
    gh api "repos/$1/compare/$2...$3" --jq '.status' 2>/dev/null)"
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
  # Baseline: the base installation token. A fork PR addresses a repo other than
  # the installed base ($1 is the head repo), and the base token may not read a
  # private fork: that 404 reads as commit-absent and makes reply_defers_on_
  # unreachable_fix defer the thread forever (ADR 0028, decision 2).
  local inst="$PRA_INSTALLATION_ID" head_inst
  # When the App is also installed on the head repo, read the commit under that
  # installation's own token. app_installation_id returns 0 only when installed,
  # so the `if` takes the head token on rc 0 and keeps the base on rc 1 (not
  # installed) or rc 2 (could-not-check). rc 1 is the common public-fork case the
  # base can still read; rc 2 is transient, so falling back re-probes next cycle
  # rather than deferring the thread on a one-off blip. Testing the substitution
  # with `if` also keeps it safe under `set -e` when the probe exits non-zero.
  if head_inst="$(app_installation_id "${1%%/*}" "${1##*/}" "$PRA_APP_ID")" &&
    [[ -n "$head_inst" ]]; then
    inst="$head_inst"
  fi

  run_with_app_token "$PRA_APP_ID" "$inst" \
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

# log_route_ledger_rollup
# One line summarising the routing ledger, printed when the daemon starts
# (#219). The ledger answers whether routing the code role is worth building,
# and an answer nobody reads is the same as no answer: a foreground run leaves
# no file to grep, and remembering to check a path is not a plan. Startup is the
# one moment the operator is already looking at this output.
#
# Silent until the ledger exists, so a fresh install prints nothing.
log_route_ledger_rollup() {
  local ledger total prose spent unknown
  ledger="$(_state_dir)/route-observations.jsonl"
  [[ -r "$ledger" ]] || return 0
  total="$(grep -c . "$ledger" 2>/dev/null || printf 0)"
  [[ "$total" -gt 0 ]] || return 0
  prose="$(grep -c '"verdict":"prose-only"' "$ledger" 2>/dev/null || printf 0)"
  # A null cost is unknown, not free, so it is counted apart rather than summed.
  read -r spent unknown < <(awk -F'"cost_usd":' '/"verdict":"prose-only"/ {
      if ($2 ~ /^null/) unknown++; else sum += $2 + 0
    } END { printf "%.2f %d\n", sum, unknown }' "$ledger")
  ((unknown == 0)) || spent="${spent} (cost unknown for ${unknown} of them)"
  log_info "routing ledger (#219): ${prose}/${total} reviews were prose-only, \$${spent} spent on them"
}

# record_route_observation <pr-url> <sha> <verdict> [cost]
# Append one line to the routing ledger, with cost_usd null when no agent
# reported a cost. A terminal log line answers the routing question only for
# whoever is watching that terminal, and the daemon's foreground mode has no
# file to grep afterward, so the observation goes somewhere durable instead.
# Best-effort: an unwritable ledger must never fail a review that otherwise
# succeeded.
#
# Read it with:
#   jq -r '[.verdict, (.cost_usd|tostring)] | @tsv' ~/.pr-review-agent/route-observations.jsonl
record_route_observation() {
  local pr="$1" sha="$2" verdict="$3" cost="$4" dir ledger
  dir="$(_state_dir)"
  [[ -d "$dir" ]] || return 0
  ledger="$dir/route-observations.jsonl"
  # One record per commit reviewed. A failed tick logs its cost on the way out
  # and the next tick retries the same sha, so appending unconditionally would
  # count that commit twice and bend the prose-only share the ledger exists to
  # measure. First write wins, which means a failure's partial cost is what
  # survives for a commit that later succeeded: the share stays honest and the
  # spend reads low, the cheaper of the two errors here.
  if [[ -r "$ledger" ]] && grep -q "\"sha\":\"${sha}\"" "$ledger" 2>/dev/null; then
    return 0
  fi
  printf '{"ts":"%s","pr":"%s","sha":"%s","verdict":"%s","cost_usd":%s}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$pr" "$sha" "$verdict" "${cost:-null}" \
    >>"$ledger" 2>/dev/null || true
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

# _app_get <jwt> <url>
# GETs a JWT-authenticated App endpoint, printing "<body>\n<http_code>". Returns
# non-zero only on a transport failure (network, DNS, timeout), which it logs
# with the url; a caller maps the http_code and reads the body itself. Shared by
# the three App GET probes so the curl flags, timeouts, and stderr capture live
# in one place. Not for _mint_installation_token, which POSTs a different shape.
_app_get() {
  local jwt="$1" url="$2" response stderr_capture
  stderr_capture="$(mktemp -t pr-review-app.XXXXXX)"
  if ! response="$(curl -sS -w '\n%{http_code}' \
    --connect-timeout 10 --max-time 30 \
    -H "Authorization: Bearer ${jwt}" \
    -H "Accept: application/vnd.github+json" \
    "$url" 2>"$stderr_capture")"; then
    log_err "GET ${url} failed: $(<"$stderr_capture")"
    rm -f "$stderr_capture"
    return 1
  fi
  rm -f "$stderr_capture"
  printf '%s' "$response"
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

  jwt="$(_app_jwt "$app_id")" || return 2
  response="$(_app_get "$jwt" "https://api.github.com/repos/${owner}/${repo}/installation")" || return 2

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
  # A 200 body that parses but carries no `id` skips the guard above, and rc=2
  # keeps the repo in the watch list, so without a line here the operator has
  # nothing to correlate the per-PR failures against.
  if [[ -z "$id" ]]; then
    log_err "installation probe for ${owner}/${repo} answered 200 with no installation id"
    return 2
  fi
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

# app_auth_init <owner> <repo> <app-id>
# Resolves everything one repo's authenticated work needs from a single probe,
# and sets it as non-exported globals the per-PR code reads:
#   PRA_APP_ID           the App id, echoed back so callers thread one value
#   PRA_INSTALLATION_ID  for run_with_app_token / _gh_token
#   PRA_BOT_LOGIN_REST   "<slug>[bot]", the author login on REST payloads
#   PRA_BOT_LOGIN_GQL    "<slug>", the author login on GraphQL payloads
# The two login forms are how the daemon recognizes its own artifacts under App
# identity (ADR 0036 decision 8): REST comments carry the [bot] suffix, GraphQL
# author nodes do not. Both come from the installation response's app_slug, so
# no extra call. Exit codes match app_installation_id (0 installed / 1 not /
# 2 could-not-check) so a caller can skip or fail the same way.
app_auth_init() {
  local owner="$1" repo="$2" app_id="$3" jwt response http_code body id slug

  jwt="$(_app_jwt "$app_id")" || return 2
  response="$(_app_get "$jwt" "https://api.github.com/repos/${owner}/${repo}/installation")" || return 2

  http_code="$(tail -1 <<<"$response")"
  case "$http_code" in
    200) ;;
    404) return 1 ;;
    *)
      log_err "installation probe for ${owner}/${repo} answered ${http_code}"
      return 2
      ;;
  esac

  body="$(sed '$d' <<<"$response")"
  if ! id="$(jq -r '.id // empty' <<<"$body" 2>/dev/null)"; then
    log_err "installation probe for ${owner}/${repo} returned unparseable JSON"
    return 2
  fi
  slug="$(jq -r '.app_slug // empty' <<<"$body")"
  if [[ -z "$id" || -z "$slug" ]]; then
    log_err "installation probe for ${owner}/${repo} answered 200 without an id or app_slug"
    return 2
  fi

  PRA_APP_ID="$app_id"
  PRA_INSTALLATION_ID="$id"
  # Read by scripts that source lib.sh (the step-3 wiring), invisible to the
  # linter from here.
  # shellcheck disable=SC2034
  PRA_BOT_LOGIN_REST="${slug}[bot]"
  # shellcheck disable=SC2034
  PRA_BOT_LOGIN_GQL="$slug"
}

# app_auth_warm
# Warms the process-lifetime token cache in the current shell, so the wrapped
# calls below it (most of which run inside command substitutions, where a mint
# would land in a doomed subshell) reuse one token instead of minting each.
# Call once per entry-point process, in its main shell, after app_auth_init.
app_auth_warm() {
  _gh_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" || return 1
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
# For a per-call cap, pass `--timeout <secs>` after the two ids: it wraps the
# command in `run_with_timeout` from inside, after the mint. Do not wrap this
# call in `run_with_timeout` from outside instead. Since #253 that runs the
# command as a background job, and a background job is a subshell, so
# `_gh_token`'s cache assignment dies with it and every wrapped call re-mints:
# the bug #252 fixed, reached through a different door. The mint itself is
# bounded by curl's own --max-time; the whole dispatch is bounded by poll.sh's
# `run_with_pr_timeout`, which is how the daemon's other gh calls are bounded.
run_with_app_token() {
  local app_id="$1" installation_id="$2" token command timeout_secs=""
  shift 2

  if [[ "${1:-}" == "--timeout" ]]; then
    timeout_secs="${2:-}"
    if [[ ! "$timeout_secs" =~ ^[0-9]+$ ]]; then
      log_err "run_with_app_token --timeout needs a whole number of seconds, got '${timeout_secs}'"
      return 1
    fi
    shift 2
  fi

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
      log_err "run_with_app_token refuses '${command}': only gh and the daemon's .py helpers may hold an App token (ADR 0036 decision 5)."
      return 1
      ;;
  esac

  if ! _gh_token "$app_id" "$installation_id" || [[ -z "$_GH_TOKEN_VALUE" ]]; then
    log_err "no App installation token for installation ${installation_id}: refusing to run '$1' unauthenticated"
    return 1
  fi
  token="$_GH_TOKEN_VALUE"

  if [[ -n "$timeout_secs" ]]; then
    GH_TOKEN="$token" run_with_timeout "$timeout_secs" "$@"
  else
    GH_TOKEN="$token" "$@"
  fi
}

# flatten_pages
# Merges `gh api --paginate` stdout into one flat JSON array. `--paginate`
# emits one array per 100-item page, concatenated (`[...][...]`), which is not
# itself valid input for a single-array jq parse (#195). Keep this out of the
# gh command substitution so gh's own exit code stays observable.
flatten_pages() {
  jq -s 'add // []'
}

# The PR fields the daemon reads, as one list so a new field is requested in one
# place. It is the union of what the two entry points read off the blob, and the
# reply path pays for the extra fields it does not read: one wider response beats
# two `--json` lists that drift apart.
readonly PR_METADATA_FIELDS="id,headRepository,headRepositoryOwner,headRefName,headRefOid,baseRefName,title,body,closingIssuesReferences,commits"

# derive_pr_metadata <pr-url>
# Prints one PR's metadata as the JSON gh returns, with gh's own field names, so
# callers read it with the same jq filters they would write against `gh pr view`.
# Returns 1 without printing when the fetch fails or the blob is unusable.
#
# The guard is here rather than in each caller because both need the same four
# head fields to clone and anchor at HEAD, and the case that breaks them is one
# gh reports as success: a closed PR whose fork is deleted answers 200 with the
# head fields empty. Callers keep their own exit path; this returns, never exits.
derive_pr_metadata() {
  local pr_url="$1" meta
  if ! meta="$(run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
    gh pr view "$pr_url" --json "$PR_METADATA_FIELDS")"; then
    log_err "gh pr view failed for $pr_url"
    return 1
  fi
  if ! jq -e '(.headRepositoryOwner.login // "") != ""
    and (.headRepository.name // "") != ""
    and (.headRefName // "") != ""
    and (.headRefOid // "") != ""' <<<"$meta" >/dev/null 2>&1; then
    log_err "gh pr view returned incomplete metadata for $pr_url (closed PR with deleted fork?)"
    return 1
  fi
  printf '%s\n' "$meta"
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
  if ! reviews_json="$(run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
    gh api --paginate "repos/${owner}/${repo}/pulls/${pr}/reviews" 2>"$stderr_capture")"; then
    log_err "sentinel discovery: gh api .../pulls/${pr}/reviews failed: $(<"$stderr_capture")"
    rm -f "$stderr_capture"
    return 2
  fi
  # Comments are best-effort: a failure here degrades to reviews-only rather
  # than escalating to a return-2 skip, since reviews (the primary source)
  # already succeeded.
  if ! comments_json="$(run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
    gh api --paginate "repos/${owner}/${repo}/issues/${pr}/comments" 2>"$stderr_capture")"; then
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
  local label="${1:-}" dir pool_size poll_interval i slot_path start_ts waited suffix polled=0
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
          suffix=""
          # Report a wait only when the poll loop actually blocked, keyed off the
          # event rather than elapsed seconds. Second-resolution `date +%s` reads
          # an immediate acquire that straddles a boundary (start at .99, claim at
          # the next .01) as a full second, which would report a wait that never
          # happened (#228).
          if ((polled)); then
            waited=$(($(date +%s) - start_ts))
            suffix=" (waited ${waited}s)"
          fi
          log_info "${label}: acquired slot ${i}/${pool_size}${suffix}"
        fi
        printf '%s\n' "$slot_path"
        return 0
      fi
      i=$((i + 1))
    done
    polled=1
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

# acquire_claude_slots <count> [label]
# All-or-nothing multi-slot acquire for the orchestrator dispatch (#299, ADR
# 0038 amended): one process running <count> hidden role sessions charges
# <count> slots, so the pool keeps bounding concurrent API sessions. Clamped to
# the pool size so a pool smaller than the role count still runs (undercharged,
# logged) instead of waiting forever. All-or-nothing because two concurrent
# orchestrators each holding a partial claim while blocking on the remainder
# would deadlock the pool; a partial claim is released before the retry sleep.
# Prints the claimed slot lock paths, newline-separated; release them together
# with release_claude_slots. Lives in lib.sh so test_claude_slot.py can source
# it (ADR 0026).
acquire_claude_slots() {
  local count="$1" label="${2:-}" pool_size slot_path i p
  local poll_interval="${CLAUDE_SLOT_POLL_SECONDS:-2}"
  local claimed=() start_ts polled=0 waited suffix
  mkdir -p "$(_state_dir)"
  pool_size="$(_slot_pool_size)"
  if ((count > pool_size)); then
    [[ -n "$label" ]] && log_info "${label}: pool size ${pool_size} < ${count} requested, charging ${pool_size}"
    count="$pool_size"
  fi
  # A zero request returns before the loop: `printf '%s\n' "${claimed[@]}"` on
  # an empty array is an unbound-variable exit under bash 3.2's set -u, and
  # there is nothing to claim anyway.
  ((count > 0)) || return 0
  start_ts="$(date +%s)"
  while true; do
    claimed=()
    i=1
    while ((i <= pool_size && ${#claimed[@]} < count)); do
      if slot_path="$(_try_claim_slot "$i")"; then
        claimed+=("$slot_path")
      fi
      i=$((i + 1))
    done
    if ((${#claimed[@]} == count)); then
      if [[ -n "$label" ]]; then
        suffix=""
        # Same event-keyed wait reporting as acquire_claude_slot (#228).
        if ((polled)); then
          waited=$(($(date +%s) - start_ts))
          suffix=" (waited ${waited}s)"
        fi
        log_info "${label}: acquired ${count}/${pool_size} slots${suffix}"
      fi
      printf '%s\n' "${claimed[@]}"
      return 0
    fi
    # Partial claim: release before sleeping (never hold-and-wait). The empty
    # check keeps bash 3.2's set -u from treating an empty array expansion as
    # an unbound variable.
    if ((${#claimed[@]})); then
      for p in "${claimed[@]}"; do rm -f "$p"; done
    fi
    polled=1
    sleep "$poll_interval"
  done
}

# release_claude_slots <newline-separated-slot-lock-paths>
# Plural sibling of release_claude_slot: removes every claimed lock. No-op on
# an empty argument so a cleanup path can call it unconditionally.
release_claude_slots() {
  local paths="${1:-}" p
  [[ -n "$paths" ]] || return 0
  while IFS= read -r p; do
    [[ -n "$p" ]] && rm -f "$p"
  done <<<"$paths"
  return 0
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
# in review-pr.sh, so a test can source this file and assert the fields (ADR 0026).
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

# extract_category <stderr-file>
# Recovers the `category=<slug>` first stderr line the Python pipeline stages emit
# on failure (the wire contract documented with the failure categories above).
# Falls back to the shared FAIL_UNKNOWN so the structured failure line is always
# populated.
extract_category() {
  local stderr_path="$1"
  local cat
  cat="$(grep -m1 '^category=' "$stderr_path" 2>/dev/null | cut -d= -f2 || true)"
  [[ -n "$cat" ]] && printf '%s' "$cat" || printf '%s' "$FAIL_UNKNOWN"
}

# extract_session_limit_deadline <stderr-file>
# Parses the `session_limit_deadline=<epoch>` second stderr line merge_findings.py
# emits alongside a session-limit category. Empty means the sentinel was there
# but its reset time was unreadable, which the pause treats as its fixed fallback.
extract_session_limit_deadline() {
  local stderr_path="$1"
  grep -m1 '^session_limit_deadline=' "$stderr_path" 2>/dev/null | cut -d= -f2- || true
}

# merge_role_payloads <harness> <transcript> <author-out> <stderr-out> <payload>...
# Unions the role payloads into <author-out> with merge_findings.py, which also
# probes the orchestrator transcript for Claude's session-limit sentence. On
# failure it sets MERGE_CATEGORY and returns 1. A session-limit classification
# under claude pauses polling and sets STATUS_PAUSE_UNTIL to the resume time;
# under codex it counts as all-lenses-failed, because a Codex quota hit never
# writes that sentence, so only text a PR induced a role to write can match it.
merge_role_payloads() {
  local harness="$1" probe="$2" out="$3" err="$4"
  shift 4
  MERGE_CATEGORY=""
  if python3 "$(dirname "${BASH_SOURCE[0]}")/merge_findings.py" --no-style \
    --session-limit-probe "$probe" "$@" >"$out" 2>"$err"; then
    return 0
  fi
  cat "$err" >&2
  MERGE_CATEGORY="$(extract_category "$err")"
  if [[ "$MERGE_CATEGORY" == session-limit && "$harness" != claude ]]; then
    log_info "ignoring a session-limit classification under ${harness}; failing the review without pausing polling"
    MERGE_CATEGORY=all-lenses-failed
  elif [[ "$MERGE_CATEGORY" == session-limit ]]; then
    # Every lens hit the subscription quota, so every PR this cycle and the next
    # would hit the same wall; pause polling until the reset instead.
    STATUS_PAUSE_UNTIL="$(session_pause_write "$(extract_session_limit_deadline "$err")")"
    log_info "session limit hit, pausing polling until $(format_clock_time "$STATUS_PAUSE_UNTIL")"
  fi
  return 1
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
# reply), so the rest of the thread need not be read. Reads every page of threads,
# since a verdict from the first page alone misses an open finding past it.
# Best-effort: prints `[]` and returns non-zero when any page fails or the pages
# do not end, so the caller degrades to "no candidates".
fetch_open_review_threads() {
  local owner="$1" repo="$2" pr="$3"
  local resp pages="" page more cursor after_args=()
  # Ten pages of 100 is far past any real PR; the cap stops a cursor that never
  # advances, and the fetch then fails rather than return a list cut short.
  for ((page = 0; page < 10; page++)); do
    # $owner/$repo/$pr/$after are GraphQL variables, not shell vars; keep them literal.
    # shellcheck disable=SC2016
    if ! resp="$(run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
      gh api graphql \
      -f query='query($owner:String!,$repo:String!,$pr:Int!,$after:String){
        repository(owner:$owner,name:$repo){
          pullRequest(number:$pr){
            reviewThreads(first:100,after:$after){
              pageInfo{ hasNextPage endCursor }
              nodes{
                id isResolved path originalLine originalStartLine line startLine
                comments(first:1){ nodes{ id url author{ login } body } }
              }
            }
          }
        }
      }' -f owner="$owner" -f repo="$repo" -F pr="$pr" \
      ${after_args[@]+"${after_args[@]}"} 2>/dev/null)" ||
      ! more="$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo
        | "\(.hasNextPage // false) \(.endCursor // "")"' <<<"$resp")"; then
      break
    fi
    pages+="$resp"$'\n'
    read -r more cursor <<<"$more"
    if [[ "$more" != "true" ]]; then
      jq -cs '[.[] | .data.repository.pullRequest.reviewThreads.nodes[]
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
        }]' <<<"$pages"
      return
    fi
    [[ -n "$cursor" ]] || break
    after_args=(-f "after=${cursor}")
  done
  printf '[]'
  return 1
}

# render_review_footer <app-slug> <head-sha>
# The review body's sign-off (ADR 0036 decisions 3, 4a). One line linking the App
# name to its profile page (github.com/apps/<slug>), which is where attribution
# lives: the footer names the App, the profile names its owner. The canonical
# `youshallnotmerge` App draws a themed line from a fixed pool; every other
# operator's App gets one plain safety line, because the flavor is inseparable
# from that name. The pool rotates by the head SHA: the review body is posted once
# per SHA and never edited in place (only the Status comment is), so a per-SHA key
# is stable within a review and varies across successive reviews on a PR. That is
# the variety the trail-position idea aimed at, without a read on the post path. A
# short or absent SHA (dry-run) falls to index 0. The leading rule detaches the
# footer from whatever ends the body.
render_review_footer() {
  local slug="$1" head_sha="$2" link idx
  [[ -n "$slug" ]] || slug="pr-review-agent"
  link="https://github.com/apps/${slug}"
  if [[ "$slug" != "youshallnotmerge" ]]; then
    printf '\n\n---\n\n🤖 _Automated review by [%s](%s)._' "$slug" "$link"
    return 0
  fi
  local pool=(
    "You shall not merge until every thread is resolved."
    "A wizard reviews precisely when it means to."
    "One does not simply merge into main."
    "Even the smallest diff can change the course of the codebase."
    "Not all those who refactor are lost."
    "Keep it secret, keep it safe: the token, never the diff."
    "Fly, you fools. The tests are red."
    "All we have to decide is what to do with the diff given us."
    "Speak friend, and merge."
    "Even the wise cannot see all ends of this diff."
    "The board is set, the threads are moving."
    "A red build is a warning, not a defeat."
    "The diff passes to the maintainer now."
    "Many that merge deserve review; some that fail deserve a second look."
    "You cannot pass without a green build."
    "I have no memory of this file."
    "There is some good in this codebase, and it is worth reviewing."
    "Not all tears are an evil, and not all failures are red."
    "That still only counts as one commit."
    "Merge in haste, repent at leisure."
    "The way is shut until every thread is resolved."
    "Little by little, one refactors the journey."
  )
  idx=0
  # Hex-prefix arithmetic; the guard keeps a non-hex or short sha at index 0.
  [[ "$head_sha" =~ ^[0-9a-fA-F]{8} ]] && idx=$((16#${head_sha:0:8} % ${#pool[@]}))
  printf '\n\n---\n\n🧙 _%s_ · [%s](%s)' "${pool[$idx]}" "$slug" "$link"
}

# Marker identifying the agent's edit-in-place review-status comment (#60).
# find_status_comment keys on it to reuse the one comment across ticks rather
# than post a second. Distinct from the sha sentinel, which lives in the Review
# body and drives dedup; this marker never does.
STATUS_COMMENT_MARKER='<!-- pr-review-agent:status -->'

# status_sha_link <repo-url> <head-oid>
# Renders the head SHA as a short, backtick-wrapped markdown link to its commit
# page on the HEAD repo (the same repo the finding blob links target, so fork
# PRs resolve correctly). Display is the 7-char short SHA GitHub itself shows; the
# href uses the full SHA so GitHub resolves it unambiguously.
status_sha_link() {
  local repo_url="$1" head_oid="$2"
  # printf format is literal markdown; the values fill the %s (not shell expansion).
  # shellcheck disable=SC2016
  printf '[`%s`](%s/commit/%s)' "${head_oid:0:7}" "$repo_url" "$head_oid"
}

# status_scope_link <repo-url> <last-sha> <head-oid> <base-ref>
# Renders the review scope as an explicit commit range. A first review (empty
# last-sha) spans the PR base to HEAD, both named; a re-review spans the last
# reviewed SHA to HEAD, wrapped in a /compare link (GitHub compare takes THREE
# dots between full SHAs). The base ref is the left endpoint on a first review and
# trailing context on a re-review, whose diff is last..HEAD and where the base
# only says which branch the PR sits on (a stacked PR's base is not the default
# branch). Targets the HEAD repo for fork correctness. Empty base-ref drops the
# base mention.
status_scope_link() {
  local repo_url="$1" last_sha="$2" head_oid="$3" base_ref="$4"
  # printf formats below are literal markdown; values fill the %s, no shell expansion.
  if [[ -z "$last_sha" ]]; then
    if [[ -n "$base_ref" ]]; then
      # shellcheck disable=SC2016
      printf 'files changed between the PR base (`%s`) and `%s`' "$base_ref" "${head_oid:0:7}"
    else
      printf 'files changed in the PR'
    fi
  else
    # shellcheck disable=SC2016
    printf 'files changed [between `%s` and `%s`](%s/compare/%s...%s)' \
      "${last_sha:0:7}" "${head_oid:0:7}" "$repo_url" "$last_sha" "$head_oid"
    # shellcheck disable=SC2016
    [[ -n "$base_ref" ]] && printf ' (base: `%s`)' "$base_ref"
  fi
}

# render_status_headline <app-slug> <state> <sha-link> [head-sha]
# The status comment's first line. <state> is reviewing | pass | block. The
# canonical youshallnotmerge App themes it: a wizard verb over a SHA-keyed emoji,
# and the gate verdict "you shall pass" / "you shall not pass" (ADR 0036 4a). The
# verdict is binary, not a count: the finding tally stays in the index rollup
# below, so the head-line duplicates nothing (ADR 0020). Every other slug gets the
# plain functional head-line. The emoji rotates by <head-sha> so a commit's line
# is stable across the status comment's many in-place edits, varying across
# commits; a short or absent sha falls to index 0.
render_status_headline() {
  local slug="$1" state="$2" sha_link="$3" head_sha="${4:-}" idx=0
  if [[ "$slug" != "youshallnotmerge" ]]; then
    case "$state" in
      reviewing) printf '👀 Reviewing %s…' "$sha_link" ;;
      *) printf '✅ Reviewed %s' "$sha_link" ;;
    esac
    return 0
  fi
  [[ "$head_sha" =~ ^[0-9a-fA-F]{7} ]] && idx=$((16#${head_sha:0:7} % 2))
  case "$state" in
    reviewing)
      local prog=(🔮 🧙)
      printf '%s Reviewing %s…' "${prog[$idx]}" "$sha_link"
      ;;
    pass)
      local ok=(🪄 ✨)
      printf '%s %s: you shall pass' "${ok[$idx]}" "$sha_link"
      ;;
    block)
      local no=(🛑 🔥)
      printf '%s %s: you shall not pass' "${no[$idx]}" "$sha_link"
      ;;
  esac
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
  # Trail sits below the file list: scope and index stay the eye's first stop
  # (current state), the trail reads as an appendix (history).
  [[ -n "$trail_block" ]] && body+=$'\n\n'"$trail_block"
  # The hidden sentinel and Status markers last (both HTML comments, neither meant
  # for the reader's eye). No visible provenance tag: the Status comment posts
  # under the bot's own login, which carries who-wrote-this (ADR 0036).
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

# find_status_comment <owner> <repo> <pr-number> <operator>
# Prints the id of the operator's status comment (the one carrying
# STATUS_COMMENT_MARKER) so a re-review edits it rather than posting a second
# (#60). Best-effort: returns 0 even on gh failure, falling back to a fresh
# post. `last` wins if more than one slipped through.
find_status_comment() {
  local owner="$1" repo="$2" pr="$3" operator="$4"
  [[ -n "$operator" ]] || return 0
  run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
    gh api "repos/${owner}/${repo}/issues/${pr}/comments" --paginate \
    --jq ".[] | select(.user.login == \"${operator}\") | select(.body | contains(\"${STATUS_COMMENT_MARKER}\")) | .id" \
    2>/dev/null | tail -1 || true
}

# post_status_comment <owner> <repo> <pr-number> <body>
# Posts a new issue comment and prints its id. Best-effort: prints nothing and
# returns 0 on failure, so a missing status comment never aborts the review.
post_status_comment() {
  local owner="$1" repo="$2" pr="$3" body="$4"
  run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
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
  run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
    gh api "repos/${owner}/${repo}/issues/comments/${comment_id}" --jq '.body' 2>/dev/null || true
}

# edit_status_comment <owner> <repo> <comment-id> <body>
# Edits an issue comment in place; a no-op on an empty id. This is the most
# user-visible write in the whole pipeline (the reviewed/failed state an
# operator actually reads), so it gets a few quick retries for a transient
# network blip (dogfood-observed: a laptop sleep/resume cycle left `gh api`
# failing right at this call) before giving up. Still best-effort overall: a
# review that already landed must never be aborted over a cosmetic status edit,
# so this always returns 0. An exhausted retry is logged, since a silent failure
# here used to leave a PR's status comment stuck on a stale "Reviewing" state
# indefinitely (the daemon already marks that SHA reviewed, so nothing else ever
# retries this specific edit) with no trace in .daemon.log to explain it.
edit_status_comment() {
  try_edit_status_comment "$@" || true
}

# try_edit_status_comment <owner> <repo> <comment-id> <body>
# edit_status_comment's retried write, returning 1 when it did not land, for a
# caller whose next write must wait on this one.
try_edit_status_comment() {
  local owner="$1" repo="$2" comment_id="$3" body="$4"
  local attempt sleep_secs="${STATUS_EDIT_RETRY_SLEEP_SECONDS:-2}"
  [[ -n "$comment_id" ]] || return 0
  for attempt in 1 2 3; do
    run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
      gh api -X PATCH "repos/${owner}/${repo}/issues/comments/${comment_id}" \
      -f body="$body" >/dev/null 2>&1 && return 0
    [[ "$attempt" -lt 3 ]] && sleep "$sleep_secs"
  done
  log_info "status comment edit failed after 3 attempts (comment ${comment_id})"
  return 1
}

# --- checks row (#308) -------------------------------------------------------
#
# The second review surface, and the one a reader scans before merging. It
# carries state alone: in_progress while the review runs, then one conclusion.
# Everything a reader has to read (findings index, scope, reviewed-SHAs trail)
# stays in the status comment above, so the row restates nothing and stays
# scannable next to the repo's other checks.

# The name the checks row shows, and the string an operator types into branch
# protection to require the review. Fixed rather than derived from the App slug,
# which varies per fork: a required-check rule names the string, so deriving it
# would rename the rule out from under the operator.
CHECK_RUN_NAME="review"

# start_check_run <owner> <repo> <head-sha> [details-url]
# Opens the in_progress check run for this review and prints its id. This is the
# pickup announcement: the row shows the review is running from here until it is
# concluded. details-url is where the row's "Details" link lands, which is the
# status comment (the surface carrying what this row deliberately omits).
#
# Created on the base repo because that is where the App is installed and where
# the PR's checks row lives. Best-effort like the status comment: prints nothing
# and returns 0 on failure, so a repo whose installation lacks `checks: write`
# still gets its review.
start_check_run() {
  local owner="$1" repo="$2" head_sha="$3" details_url="${4:-}"
  local payload
  payload="$(jq -n --arg name "$CHECK_RUN_NAME" --arg sha "$head_sha" --arg url "$details_url" \
    '{name: $name, head_sha: $sha, status: "in_progress"}
     | if $url == "" then . else .details_url = $url end')"
  run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
    gh api -X POST "repos/${owner}/${repo}/check-runs" --input - --jq '.id' \
    <<<"$payload" 2>/dev/null || true
}

# complete_check_run <owner> <repo> <check-run-id> <conclusion> <title> <summary>
# Concludes a live check run, ending the in_progress state the row shows.
# No-ops on an empty id (no run was opened).
#
# Retried like edit_status_comment (ADR 0027) and for a stronger reason: a run
# left in_progress can hold a merge back where a stale comment only misinforms,
# so a dropped conclude is worth three attempts. Still best-effort overall, since
# a landed review must never be aborted over its checks row, but an exhausted
# retry is logged: the state it leaves behind is the one an operator has to clear
# by hand.
complete_check_run() {
  try_complete_check_run "$@" || true
}

# try_complete_check_run <owner> <repo> <check-run-id> <conclusion> <title> <summary>
# complete_check_run's retried write, returning 1 when it did not land, for a
# caller that reports whether the run changed.
try_complete_check_run() {
  local owner="$1" repo="$2" check_run_id="$3" conclusion="$4" title="$5" summary="$6"
  local attempt sleep_secs="${CHECK_RUN_RETRY_SLEEP_SECONDS:-2}" payload
  [[ -n "$check_run_id" ]] || return 0
  payload="$(jq -n --arg conclusion "$conclusion" --arg title "$title" --arg summary "$summary" \
    '{status: "completed", conclusion: $conclusion, output: {title: $title, summary: $summary}}')"
  for attempt in 1 2 3; do
    run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
      gh api -X PATCH "repos/${owner}/${repo}/check-runs/${check_run_id}" --input - \
      <<<"$payload" >/dev/null 2>&1 && return 0
    [[ "$attempt" -lt 3 ]] && sleep "$sleep_secs"
  done
  log_info "check run ${check_run_id} not concluded after 3 attempts (left in its previous state)"
  return 1
}

# conclude_review_check <owner> <repo> <check-run-id> <state>
# Concludes the `review` run on a block|pass verdict, returning 1 when the write
# did not land. The titles say which state the row is in and nothing more; the
# count, the findings, and the trail stay one click away in the status comment.
conclude_review_check() {
  local title="No findings open on this commit"
  [[ "$4" == "block" ]] && title="Findings open on this commit"
  try_complete_check_run "$1" "$2" "$3" "$(check_conclusion_for_state "$4")" "$title" \
    "The findings index, the review scope, and the reviewed-SHAs trail are in the review status comment."
}

# count_open_findings <threads-file> <bot-login-gql>
# The verdict's one input (ADR 0040 decision 4): how many of the bot's finding
# threads in a fetch_open_review_threads file are still open. Prints 0 when the
# file is unreadable.
count_open_findings() {
  jq --arg op "$2" \
    '[.[] | select(.root_author == $op and ((.is_resolved // false) | not))] | length' \
    "$1" 2>/dev/null || printf 0
}

# review_state_for_open_threads <open-thread-count>
# The review's binary verdict, `block` or `pass`, rendered by the status head-line
# (ADR 0036 4a) and mapped to the checks row below.
#
# Open daemon finding threads are the whole input, because a gate must satisfy
# "fix it and it opens" (ADR 0040 decision 4). A finding with no thread has
# nothing to resolve, so gating on one leaves the row red permanently, which is
# what PR #312 showed. Severity is deliberately not an input either: the grading
# orders reading, not merging.
review_state_for_open_threads() {
  if [[ "${1:-0}" -gt 0 ]]; then
    printf 'block'
  else
    printf 'pass'
  fi
}

# check_conclusion_for_state <state>
# Maps the review's verdict, the same block|pass the status head-line renders, to
# a GitHub check-run conclusion. One verdict feeds both surfaces so they cannot
# disagree.
#
# `block` is `failure`, not `neutral`: GitHub treats neutral as a pass, so a
# neutral block would decide the gating question for the operator by making the
# check unusable as a required check. Whether the review holds a merge back is
# branch protection, which is theirs to set; this reports the verdict either way.
# Not `action_required` either, whose call-to-action framing overstates a review
# whose findings may all be nits.
#
# Anything else, including the review that crashed before it had a verdict, is
# `neutral`: no verdict was reached, so the row must state that without gating a
# merge on a daemon-side failure the author cannot act on.
check_conclusion_for_state() {
  case "$1" in
    pass) printf 'success' ;;
    block) printf 'failure' ;;
    *) printf 'neutral' ;;
  esac
}

# refresh_review_verdict <owner> <repo> <pr-number> <head-sha> <head-repo-url>
# Brings the status comment and the `review` check to the verdict the PR's finding
# threads now give, after a reply pass changed them under a completed review (ADR
# 0039 decision 6). The verdict is derived once, as the review derives it, and
# each surface is written only where it differs, so a surface an earlier write
# left stale is repaired even when the other is current. Best-effort like the
# review's own writes: always returns 0.
#
# Takes the review's per-PR lock, without waiting: a review in flight reads the
# threads itself before it writes both surfaces, and holding the lock keeps one
# from starting while this rewrites them.
refresh_review_verdict() {
  local lock
  if ! lock="$(acquire_pr_lock "$1" "$2" "$3")"; then
    log_info "review in progress; it sets the verdict from the threads it reads"
    return 0
  fi
  _refresh_review_verdict "$@" || log_info "verdict refresh failed (non-fatal)"
  release_pr_lock "$lock"
  return 0
}

_refresh_review_verdict() {
  local owner="$1" repo="$2" pr="$3" head_sha="$4" head_repo_url="$5"
  local comments status comment_id old_body reviewed_sha threads_file body_file
  local state new_body current_head rc=0
  comments="$(run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
    gh api --paginate "repos/${owner}/${repo}/issues/${pr}/comments" 2>/dev/null)" || return 1
  status="$(flatten_pages <<<"$comments" |
    jq -c --arg login "$PRA_BOT_LOGIN_REST" --arg marker "$STATUS_COMMENT_MARKER" \
      '[.[] | select(.user.login == $login and ((.body // "") | contains($marker)))] | last // empty')" ||
    return 1
  if [[ -z "$status" ]]; then
    log_info "no status comment to refresh"
    return 0
  fi
  comment_id="$(jq -r '.id' <<<"$status")"
  old_body="$(jq -r '.body' <<<"$status")"
  # Only the terminal render carries the sentinel (ADR 0024), so it names the
  # commit whose verdict both surfaces show; a Reviewing or failed render has none.
  reviewed_sha="$(grep -oE 'pr-review-agent:sha:[0-9a-f]{40}' <<<"$old_body" | head -1 | cut -d: -f3 || true)"
  if [[ "$reviewed_sha" != "$head_sha" ]]; then
    log_info "status comment shows no completed review of ${head_sha:0:12}; the next review sets the verdict"
    return 0
  fi

  threads_file="$(mktemp -t pr-review-verdict.XXXXXX)"
  body_file="$(mktemp -t pr-review-verdict.XXXXXX)"
  if ! fetch_open_review_threads "$owner" "$repo" "$pr" >"$threads_file"; then
    rm -f "$threads_file" "$body_file"
    return 1
  fi
  state="$(review_state_for_open_threads "$(count_open_findings "$threads_file" "$PRA_BOT_LOGIN_GQL")")"
  printf '%s' "$old_body" >"$body_file"
  new_body="$(python3 "$(dirname "${BASH_SOURCE[0]}")/findings_index.py" \
    --threads "$threads_file" --operator "$PRA_BOT_LOGIN_GQL" --refresh-body "$body_file" \
    --headline "$(render_status_headline "$PRA_BOT_LOGIN_GQL" "$state" \
      "$(status_sha_link "$head_repo_url" "$head_sha")" "$head_sha")")" || rc=$?
  rm -f "$threads_file" "$body_file"
  if [[ "$rc" -eq 3 ]]; then
    log_info "status comment is in a form the refresh does not rewrite; it and the check stay as posted"
    return 0
  fi
  [[ "$rc" -eq 0 && -n "$new_body" ]] || return 1

  # The caller read HEAD before its agent and posting phases, so a push since then
  # would let this write the verdict of a commit that is no longer HEAD.
  if ! current_head="$(run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
    --timeout "${GH_API_CALL_TIMEOUT:-90}" \
    gh api "repos/${owner}/${repo}/pulls/${pr}" --jq '.head.sha' 2>/dev/null)" ||
    [[ -z "$current_head" ]]; then
    log_info "PR head re-read failed; the verdict is left to the next pass"
    return 0
  fi
  if [[ "$current_head" != "$head_sha" ]]; then
    log_info "PR head moved to ${current_head:0:12} during the pass; the next review sets the verdict"
    return 0
  fi

  # The renderer reads the body with its line endings normalised, so the body
  # GitHub returned is compared the same way.
  if [[ "$new_body" == "${old_body//$'\r'/}" ]]; then
    log_info "status comment already shows ${state}"
  elif try_edit_status_comment "$owner" "$repo" "$comment_id" "$new_body"; then
    log_info "status comment refreshed to ${state}"
  else
    log_info "status comment not refreshed; the check is left as it is so it never runs ahead of the comment"
    return 0
  fi
  _refresh_review_check "$owner" "$repo" "$head_sha" "$state"
}

# _refresh_review_check <owner> <repo> <head-sha> <state>
# Brings the App's newest `review` run on <head-sha>, the one the latest review
# of it opened, to <state>'s conclusion. Any completed run is brought, `neutral`
# included: the caller gets here only past the sentinel, so the review finished,
# and a `neutral` run means it stopped between its comment edit and its conclude.
# A run still in progress is left to the review that opened it.
_refresh_review_check() {
  local owner="$1" repo="$2" head_sha="$3" state="$4"
  local run run_id run_status shown conclusion
  conclusion="$(check_conclusion_for_state "$state")"
  # filter=all because the default, `latest`, is documented only as "the most
  # recent check runs", which leaves unsaid which same-name runs it drops.
  if ! run="$(run_with_app_token "$PRA_APP_ID" "$PRA_INSTALLATION_ID" \
    gh api "repos/${owner}/${repo}/commits/${head_sha}/check-runs?check_name=${CHECK_RUN_NAME}&app_id=${PRA_APP_ID}&filter=all&per_page=100" \
    --jq '.check_runs | max_by(.id) // empty | "\(.id) \(.status) \(.conclusion)"' 2>/dev/null)"; then
    log_info "check run lookup on ${head_sha:0:12} failed; the check is left as it is"
    return 0
  fi
  if [[ -z "$run" ]]; then
    log_info "no review check run on ${head_sha:0:12} to update"
    return 0
  fi
  read -r run_id run_status shown <<<"$run"
  if [[ "$run_status" != "completed" ]]; then
    log_info "check run ${run_id} is ${run_status}; the review that opened it concludes it"
  elif [[ "$shown" == "$conclusion" ]]; then
    log_info "check run ${run_id} already ${conclusion}"
  elif conclude_review_check "$owner" "$repo" "$run_id" "$state"; then
    log_info "check run ${run_id} updated from ${shown} to ${conclusion}"
  else
    log_info "check run ${run_id} not updated; it stays ${shown}"
  fi
}
