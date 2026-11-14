#!/usr/bin/env bash
# setup-model.sh: choose the agent CLI, model, and effort the review agents run
# on, check them, and write REVIEW_HARNESS, REVIEW_MODEL, and REVIEW_EFFORT to
# .env (or to the path given). Re-runnable; .env stays untouched unless every
# step passes. --help describes the flag form, --check, and --list-models.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=daemon/lib.sh disable=SC1091
source "$REPO_ROOT/daemon/lib.sh"

usage() {
  cat <<EOF
Usage:
  bash bin/setup-model.sh [ENV_FILE]
  bash bin/setup-model.sh [--harness claude|codex] [--model NAME] [--effort LEVEL]
                          [--no-test-call] [ENV_FILE]
  bash bin/setup-model.sh --check [--test-call] [ENV_FILE]
  bash bin/setup-model.sh --list-models [--harness claude|codex] [ENV_FILE]

Sets REVIEW_HARNESS, REVIEW_MODEL, and REVIEW_EFFORT in ENV_FILE, by default
.env in this checkout. With no flags it asks for each value on the terminal.
With --harness, --model, --effort, or --no-test-call it asks nothing and
reads no input.

  --harness NAME   the agent CLI, claude or codex; default: the current setting
  --model NAME     the model; required with codex; with claude, omitted means
                   the daemon default, written as an empty REVIEW_MODEL
  --effort LEVEL   one of: ${REVIEW_EFFORT_LEVELS}; with codex, one the model
                   lists; default: the daemon default
  --no-test-call   skip the one test call, the prompt "Reply with OK"
  --check          check the current setting (environment, then ENV_FILE, then
                   defaults) and write nothing
  --test-call      with --check, also make the test call
  --list-models    print the models the CLI lists, one per line; for codex,
                   each name, a tab, and the effort levels it takes
  -h, --help       print this help

Standard output carries only the result: the three lines written, the --check
report, or the model list. Messages go to standard error.

Exit status:
  0    success
  1    a check failed or a value was refused; nothing was written
  2    usage error
  ${TIMEOUT_EXIT}  a question went unanswered; nothing was written
EOF
}

usage_error() {
  log_err "$*; run bash bin/setup-model.sh --help for usage"
  exit 2
}

DOTENV=""
mode=""
write_flags=0
harness_arg=""
model_arg=""
effort_arg=""
test_call=1
check_test_call=0
while (($# > 0)); do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    --harness | --model | --effort)
      [[ $# -ge 2 && -n "$2" ]] || usage_error "$1 needs a value"
      case "$1" in
        --harness) harness_arg="$2" ;;
        --model) model_arg="$2" write_flags=1 ;;
        --effort) effort_arg="$2" write_flags=1 ;;
      esac
      shift
      ;;
    --no-test-call) test_call=0 write_flags=1 ;;
    --test-call) check_test_call=1 ;;
    --check | --list-models)
      [[ -z "$mode" || "$mode" == "$1" ]] || usage_error "--check and --list-models cannot be combined"
      mode="$1"
      ;;
    -*) usage_error "$1 is not a flag this script takes" ;;
    *)
      [[ -z "$DOTENV" ]] || usage_error "only one env file path is accepted"
      DOTENV="$1"
      ;;
  esac
  shift
done
case "$mode" in
  --check)
    [[ -z "$harness_arg" && "$write_flags" == 0 ]] ||
      usage_error "--check takes only --test-call and an env file path"
    ;;
  --list-models)
    [[ "$write_flags" == 0 && "$check_test_call" == 0 ]] ||
      usage_error "--list-models takes only --harness and an env file path"
    ;;
  *)
    [[ "$check_test_call" == 0 ]] ||
      usage_error "--test-call goes with --check; a write makes the test call unless --no-test-call is given"
    [[ -z "$harness_arg" ]] || write_flags=1
    mode=wizard
    [[ "$write_flags" == 0 ]] || mode=flags
    ;;
esac
DOTENV="${DOTENV:-$REPO_ROOT/.env}"
# Only the wizard reads answers, so every other mode, and every CLI it starts,
# reads no input from whatever stdin is.
[[ "$mode" == wizard ]] || exec </dev/null

WORK=""
OUT=""
cleanup() { rm -rf "${WORK:+$(dirname "$WORK")}" "$OUT"; }
trap cleanup EXIT

# Seconds each prompt waits. Override PR_REVIEW_ANSWER_TIMEOUT_SECONDS for tests.
ANSWER_TIMEOUT_SECONDS="${PR_REVIEW_ANSWER_TIMEOUT_SECONDS:-120}"

fail() {
  log_err "$*"
  exit 1
}

# ask <prompt> <default> [shown-default]: prints the answer, or the default on an
# empty one. Prompts go to stderr because callers capture stdout. No answer exits
# TIMEOUT_EXIT, which offer_model_setup reads as declining for now.
ask() {
  local answer
  printf '%s [%s]: ' "$1" "${3:-$2}" >&2
  if ! IFS= read -t "$ANSWER_TIMEOUT_SECONDS" -r answer; then
    printf '\n' >&2
    log_err "no answer within ${ANSWER_TIMEOUT_SECONDS} seconds, or input ended; nothing was written"
    exit "$TIMEOUT_EXIT"
  fi
  printf '%s' "${answer:-$2}"
}

# print_options <name> <description> ...: prints one numbered line per pair, with
# the descriptions starting in one column.
print_options() {
  local options=("$@") width=0 k
  for ((k = 0; k < ${#options[@]}; k += 2)); do
    ((${#options[$k]} <= width)) || width=${#options[$k]}
  done
  for ((k = 0; k < ${#options[@]}; k += 2)); do
    if [[ -n "${options[$((k + 1))]}" ]]; then
      printf '  %d) %-*s  %s\n' "$((k / 2 + 1))" "$width" "${options[$k]}" "${options[$((k + 1))]}" >&2
    else
      printf '  %d) %s\n' "$((k / 2 + 1))" "${options[$k]}" >&2
    fi
  done
}

# menu <heading> <question> <default> <name> <description> ...: lists the options
# and asks until the answer is one of them, by number or by name.
menu() {
  local heading="$1" question="$2" default="$3" answer k
  shift 3
  local options=("$@")
  printf '%s\n' "$heading" >&2
  print_options "$@"
  while true; do
    answer="$(ask "$question, a number or a name" "$default")" || exit
    if [[ "$answer" =~ ^[0-9]{1,3}$ ]] && ((10#$answer >= 1 && 10#$answer * 2 <= ${#options[@]})); then
      answer="${options[$((10#$answer * 2 - 2))]}"
    fi
    for ((k = 0; k < ${#options[@]}; k += 2)); do
      [[ "${options[$k]}" != "$answer" ]] || {
        printf '%s' "$answer"
        return
      }
    done
    printf 'Choose a number or a name from the list.\n' >&2
  done
}

# ask_model <prompt> <default>: asks until the answer is a name .env can hold,
# before any call is spent on it.
ask_model() {
  local answer
  while true; do
    answer="$(ask "$1" "$2")" || exit
    if [[ -z "$answer" ]]; then
      printf 'A model name is required.\n' >&2
    elif [[ "$answer" =~ $DOTENV_VALUE_RE ]]; then
      break
    else
      printf 'A model name holds only letters, digits, and . _ : / @ [ ] -\n' >&2
    fi
  done
  printf '%s' "$answer"
}

cli_label() {
  case "$1" in
    claude) printf 'Claude Code' ;;
    codex) printf 'Codex CLI' ;;
  esac
}

set_cli() {
  cli="$1"
  product="$(cli_label "$cli")"
  [[ "$cli" == claude ]] || product="the $product"
}

check_on_path() {
  command -v "$cli" >/dev/null 2>&1 && return
  log_err "$cli is not on PATH; install ${product}, then re-run bash bin/setup-model.sh"
  return 1
}

check_claude_login() {
  # The status JSON names the account, so only its loggedIn field is read.
  claude auth status --json 2>/dev/null | jq -e .loggedIn >/dev/null 2>&1 && return
  log_err "Claude Code is not logged in; run claude auth login"
  return 1
}

# find_real_codex: sets codex_bin to the codex every agent call runs.
find_real_codex() {
  require_real_codex || return 1
  codex_bin="$(python3 -c 'import os, sys
sys.path.insert(0, sys.argv[1])
import codex_exec
print(codex_exec.find_codex(os.environ.get("PATH", os.defpath))[0])' "$REPO_ROOT/daemon")"
}

check_codex_login() {
  "$codex_bin" login status >/dev/null 2>&1 && return
  log_err "Codex is not logged in; run codex login"
  return 1
}

# The test call runs in a directory made as the daemon makes each per-PR clone.
make_work_dirs() {
  WORK="$(make_clone_dir pr-review-setup)"
  OUT="$(make_temp_dir pr-review-setup-out)"
}

check_temp_root() {
  local temp_root
  temp_root="$(cd "$WORK/../.." && pwd -P)"
  case "$temp_root/" in
    /tmp/* | /private/tmp/* | /var/tmp/* | /private/var/tmp/*)
      log_err "Codex refuses to run in ${temp_root}, where each per-PR clone would land, because its sandbox denies the system temp directories; run export TMPDIR=\"\$HOME/.pr-review-agent/tmp\" && mkdir -p \"\$TMPDIR\" in the shell that starts the daemon, then re-run bash bin/setup-model.sh there; nothing was written"
      return 1
      ;;
  esac
}

check_codex_model() {
  resolve_review_model "$DOTENV" codex >/dev/null
}

# refuse_bad_harness [suffix]: exits 1 naming the invalid REVIEW_HARNESS and
# where it was set, because the daemon keeps reading that value.
refuse_bad_harness() {
  local source="${DOTENV}"
  [[ -z "${REVIEW_HARNESS:-}" ]] || source="the environment"
  fail "REVIEW_HARNESS is set to $(resolve_tunable REVIEW_HARNESS "$DOTENV") in ${source}, which is not claude or codex; pass --harness claude or --harness codex, or fix it there${1:-}"
}

# load_catalog: reads the account's codex models into catalog, and the ones it
# lists for choosing into slugs and descriptions, by priority; fails when the
# command does.
load_catalog() {
  local slug description
  slugs=()
  descriptions=()
  # Without --bundled the catalog is the account's own list, not the binary's.
  catalog="$("$codex_bin" debug models 2>/dev/null)" || {
    catalog=""
    return 1
  }
  # Descriptions reach the terminal, so control characters are stripped.
  while IFS=$'\t' read -r slug description; do
    [[ -n "$slug" && "$slug" =~ $DOTENV_VALUE_RE ]] || continue
    slugs+=("$slug")
    descriptions+=("$description")
  done < <(jq -r '[.models[]? | select(.visibility == "list" and (.slug | type) == "string")]
    | sort_by(.priority)[]
    | [.slug, ((.description // "") | tostring | gsub("[[:cntrl:]]"; ""))] | @tsv' <<<"$catalog" 2>/dev/null || true)
  ((${#slugs[@]} > 0)) || catalog=""
}

# usable_levels <model>: the daemon's effort levels the codex catalog lists for
# the model, space-separated, or all of them when the catalog lists none.
usable_levels() {
  local supported="" level out=""
  if [[ -n "$catalog" ]]; then
    # A level may be an object with an effort field or a plain string.
    supported="$(jq -r --arg m "$1" 'first(.models[]? | select(.slug == $m)
      | [.supported_reasoning_levels[]? | if type == "object" then .effort else . end | strings]
      | join(" ")) // ""' <<<"$catalog" 2>/dev/null)" || supported=""
  fi
  if [[ -z "$supported" ]]; then
    printf '%s' "$REVIEW_EFFORT_LEVELS"
    return
  fi
  for level in $REVIEW_EFFORT_LEVELS; do
    [[ " $supported " != *" $level "* ]] || out="${out:+$out }$level"
  done
  printf '%s' "$out"
}

check_codex_effort() {
  local levels
  levels="$(usable_levels "$model")"
  [[ " $levels " != *" $effort "* ]] || return 0
  log_err "$model on codex takes effort ${levels// /, }, not ${effort}; set REVIEW_EFFORT to one of them"
  return 1
}

run_test_call() {
  # shellcheck disable=SC2034  # run_agent reads them
  REVIEW_HARNESS="$cli" REVIEW_MODEL="$model" REVIEW_EFFORT="$effort"
  # The agent runs in an empty directory, so the call carries no code.
  printf 'Reply with OK\n' >"$OUT/prompt.txt"
  local rc=0
  (cd "$WORK" && run_agent read-only 120 "$OUT/prompt.txt" "$OUT/raw.txt" "test call") || rc=$?
  if ((rc != 0)) || [[ ! -s "$OUT/raw.txt" ]]; then
    log_err "${model} did not answer on ${cli} and nothing was written; run bash bin/setup-model.sh --check, and confirm the model name with --list-models"
    if [[ -s "$OUT/raw.txt.stderr" ]]; then
      printf 'Its last stderr lines:\n' >&2
      tail -n 5 "$OUT/raw.txt.stderr" >&2
    fi
    return 1
  fi
  printf '%s answered on %s.\n' "$model" "$cli" >&2
}

for cmd in jq python3; do
  command -v "$cmd" >/dev/null 2>&1 || fail "$cmd is not on PATH; install it, then re-run bash bin/setup-model.sh"
done

case "$harness_arg" in
  "" | claude | codex) ;;
  *) fail "--harness takes claude or codex, not ${harness_arg}; nothing was written" ;;
esac

if [[ "$mode" == --list-models ]]; then
  if [[ -n "$harness_arg" ]]; then
    set_cli "$harness_arg"
  else
    current="$(resolve_review_harness "$DOTENV" 2>/dev/null)" || refuse_bad_harness
    set_cli "$current"
  fi
  if [[ "$cli" == claude ]]; then
    printf '%s\n' "$(REVIEW_MODEL='' resolve_review_model /dev/null)"
    printf 'Claude Code has no command that lists models, so this is the daemon default; --model takes any model name or alias the claude CLI accepts.\n' >&2
    exit 0
  fi
  check_on_path || exit 1
  find_real_codex || exit 1
  load_catalog || fail "codex debug models failed, so there is no model list; check the sign-in with codex login status, then re-run"
  ((${#slugs[@]} > 0)) || fail "codex debug models listed no models; pass a model name the codex CLI accepts to --model"
  for slug in "${slugs[@]}"; do
    levels="$(usable_levels "$slug")"
    # A model with none of the daemon's levels would be refused on write.
    [[ -n "$levels" ]] || continue
    printf '%s\t%s\n' "$slug" "${levels// /,}"
  done
  exit 0
fi

if [[ "$mode" == --check ]]; then
  failed=0
  # check <name> <command...>: prints the check's result line on stdout.
  check() {
    local name="$1"
    shift
    if "$@"; then
      printf '%s: ok\n' "$name"
    else
      printf '%s: failed\n' "$name"
      failed=1
      return 1
    fi
  }
  skip() { printf '%s: not checked\n' "$1"; }
  if ! current="$(resolve_review_harness "$DOTENV" 2>/dev/null)"; then
    printf 'harness: %s\n' "$(resolve_tunable REVIEW_HARNESS "$DOTENV")"
    printf 'harness is claude or codex: failed\n'
    fail "REVIEW_HARNESS is not claude or codex; set it to one of them, or run bash bin/setup-model.sh --harness claude"
  fi
  set_cli "$current"
  model="$(resolve_tunable REVIEW_MODEL "$DOTENV")"
  [[ "$cli" == codex ]] || model="$(resolve_review_model "$DOTENV")"
  effort="$(resolve_review_effort "$DOTENV")"
  printf 'harness: %s\nmodel: %s\neffort: %s\n' "$cli" "${model:-(not set)}" "$effort"
  make_work_dirs
  codex_bin=""
  if [[ "$cli" == claude ]]; then
    if check "claude on PATH" check_on_path; then
      check "claude version" require_claude_version || true
      check "claude logged in" check_claude_login || true
    else
      skip "claude version"
      skip "claude logged in"
    fi
  else
    if check "codex on PATH" check_on_path; then
      if check "codex is the real binary" find_real_codex; then
        check "codex logged in" check_codex_login || true
      else
        skip "codex logged in"
      fi
    else
      skip "codex is the real binary"
      skip "codex logged in"
    fi
    model_ok=0
    check "model set for codex" check_codex_model && model_ok=1
    if [[ -z "$codex_bin" || "$model_ok" == 0 ]]; then
      skip "effort listed for the model"
    elif load_catalog && [[ -n "$catalog" ]]; then
      check "effort listed for the model" check_codex_effort || true
    else
      printf 'codex debug models returned no model list, so the effort was not checked.\n' >&2
      skip "effort listed for the model"
    fi
    check "per-PR clones outside the system temp directories" check_temp_root || true
  fi
  if [[ "$check_test_call" == 1 ]]; then
    if [[ "$failed" == 0 ]]; then
      check "test call" run_test_call || true
    else
      skip "test call"
    fi
  fi
  exit "$failed"
fi

[[ ! -e "$DOTENV" || -w "$DOTENV" ]] ||
  fail "${DOTENV} is not writable, so it is unchanged; make it writable with chmod u+w and re-run"

if ! current_harness="$(resolve_review_harness "$DOTENV" 2>/dev/null)"; then
  [[ "$mode" == wizard || -n "$harness_arg" ]] || refuse_bad_harness "; nothing was written"
  current_harness=claude
fi
if [[ "$mode" == wizard ]]; then
  cli="$(menu "Agent CLI:" "Agent CLI" "$current_harness" \
    claude "$(cli_label claude)" codex "$(cli_label codex)")"
  set_cli "$cli"
else
  set_cli "${harness_arg:-$current_harness}"
  # Values are refused here, before any CLI call is spent on them.
  [[ -z "$model_arg" || "$model_arg" =~ $DOTENV_VALUE_RE ]] ||
    fail "a model name holds only letters, digits, and . _ : / @ [ ] -; nothing was written"
  [[ -z "$effort_arg" || " $REVIEW_EFFORT_LEVELS " == *" $effort_arg "* ]] ||
    fail "--effort takes one of: ${REVIEW_EFFORT_LEVELS}; nothing was written"
  if [[ "$cli" == codex ]]; then
    [[ -n "$model_arg" ]] ||
      usage_error "--model is required with codex, which has no default model; bash bin/setup-model.sh --list-models --harness codex lists the names"
    REVIEW_MODEL="$model_arg" resolve_review_model "$DOTENV" codex >/dev/null || exit 1
  fi
fi

check_on_path || exit 1
make_work_dirs
if [[ "$cli" == claude ]]; then
  require_claude_version || exit 1
  check_claude_login || exit 1
else
  find_real_codex || exit 1
  check_codex_login || exit 1
  check_temp_root || exit 1
fi

current_model=""
[[ "$current_harness" != "$cli" ]] || current_model="$(resolve_tunable REVIEW_MODEL "$DOTENV")"
catalog=""
if [[ "$cli" == codex ]]; then
  load_catalog || true
  if [[ "$mode" == flags ]]; then
    model="$model_arg"
  elif ((${#slugs[@]} == 0)); then
    printf '\ncodex debug models listed no models, so type a model name the codex CLI accepts.\n' >&2
    model="$(ask_model "Model" "$current_model")"
  else
    printf '\nModels your codex account lists:\n' >&2
    model_options=()
    for i in "${!slugs[@]}"; do
      model_options+=("${slugs[$i]}" "${descriptions[$i]}")
    done
    print_options "${model_options[@]}"
    printf '  %d) type a model name\n' "$((${#slugs[@]} + 1))" >&2
    while true; do
      model="$(ask_model "Model, a number or a name" "${current_model:-1}")"
      [[ "$model" =~ ^[0-9]+$ ]] || break
      pick=$((10#$model))
      if ((pick >= 1 && pick <= ${#slugs[@]})); then
        model="${slugs[$((pick - 1))]}"
        break
      elif ((pick == ${#slugs[@]} + 1)); then
        model="$(ask_model "Model name" "$current_model")"
        break
      fi
      printf 'Choose a number or a name from the list.\n' >&2
    done
  fi
  REVIEW_MODEL="$model" resolve_review_model "$DOTENV" codex >/dev/null || exit 1
  model_out="$model"
else
  daemon_model="$(REVIEW_MODEL='' resolve_review_model /dev/null)"
  if [[ "$mode" == flags ]]; then
    model="${model_arg:-$daemon_model}"
  else
    printf '\nModel the claude CLI runs; the default is %s. From claude --help:\n' "$daemon_model" >&2
    claude --help 2>/dev/null | awk '/^ +--model /{on=1; print; next} on && /^   /{print; next} {on=0}' |
      LC_ALL=C tr -d '\000-\011\013-\037\177' >&2 || true
    model="$(ask_model "Model" "${current_model:-$daemon_model}")"
  fi
  model_out="$model"
  if [[ "$model" == "$daemon_model" ]]; then
    model_out=""
    printf 'REVIEW_MODEL stays empty, so reviews follow the daemon default, now %s.\n' "$daemon_model" >&2
  elif [[ "$model" != *claude-* ]]; then
    printf '%s is not a full model name; if it is an alias, the model it names can change when the claude CLI updates.\n' "$model" >&2
  fi
fi

levels="$(usable_levels "$model")"
[[ -n "$levels" ]] || fail "$model supports none of the effort levels the daemon accepts ($REVIEW_EFFORT_LEVELS); nothing was written"
daemon_effort="$(REVIEW_EFFORT='' resolve_review_effort /dev/null)"
if [[ "$mode" == flags ]]; then
  effort="${effort_arg:-$daemon_effort}"
  if [[ " $levels " != *" $effort "* ]]; then
    [[ -z "$effort_arg" ]] ||
      fail "$model on codex takes effort ${levels// /, }, not ${effort}; nothing was written"
    fail "$model on codex takes effort ${levels// /, }, not the default ${effort}; pass --effort with one of them; nothing was written"
  fi
else
  default_effort="$(resolve_review_effort "$DOTENV" 2>/dev/null)"
  [[ " $levels " == *" $default_effort "* ]] || default_effort="${levels%% *}"
  effort_options=()
  for level in $levels; do
    # Descriptions reach the terminal, so control characters are stripped.
    description=""
    [[ -z "$catalog" ]] || description="$(jq -r --arg m "$model" --arg l "$level" 'first(.models[]?
      | select(.slug == $m) | .supported_reasoning_levels[]? | objects | select(.effort == $l)
      | .description | strings | gsub("[[:cntrl:]]"; "")) // ""' <<<"$catalog" 2>/dev/null)" || description=""
    effort_options+=("$level" "$description")
  done
  printf '\n' >&2
  effort="$(menu "Effort levels $model takes:" "Effort" "$default_effort" "${effort_options[@]}")"
fi
effort_out="$effort"
# Under codex the effort stays written, because a later default might be a level
# the pinned model does not support.
if [[ "$cli" == claude && "$effort" == "$daemon_effort" ]]; then
  effort_out=""
  printf 'REVIEW_EFFORT stays empty, so reviews follow the daemon default, now %s.\n' "$daemon_effort" >&2
fi

printf '\n' >&2
if [[ "$cli" == codex ]]; then
  printf '%s\n' 'On a personal ChatGPT plan, content sent through Codex, including code from each per-PR clone, can be used for training by default. Check ChatGPT Settings, Data controls, "Improve the model for everyone" and Codex'"'"'s "Include environments" before reviewing private repositories.' >&2
else
  printf '%s\n' 'On a Claude consumer plan, whether your sessions can be used for training is set at claude.ai/settings/data-privacy-controls; check it before reviewing private repositories.' >&2
fi
printf '\n' >&2

if [[ "$mode" == wizard ]]; then
  test_call=0
  answer="$(menu 'Test call (the prompt "Reply with OK"):' "Test call" send \
    send "Check that ${model} answers on ${cli}" skip "Write the settings without checking")"
  [[ "$answer" != send ]] || test_call=1
fi
if [[ "$test_call" == 1 ]]; then
  run_test_call || exit 1
else
  printf '%s on %s is not verified; the first review will show whether it works.\n' "$model" "$cli" >&2
fi

set_dotenv_keys "$DOTENV" "REVIEW_HARNESS=$cli" "REVIEW_MODEL=$model_out" "REVIEW_EFFORT=$effort_out" || exit 1
printf 'REVIEW_HARNESS=%s\nREVIEW_MODEL=%s\nREVIEW_EFFORT=%s\n' "$cli" "$model_out" "$effort_out"
printf 'Wrote these lines to %s.\n' "$DOTENV" >&2
for key in REVIEW_HARNESS REVIEW_MODEL REVIEW_EFFORT; do
  [[ -z "$(printenv "$key" || true)" ]] ||
    printf '%s is also set in your shell environment, which overrides .env.\n' "$key" >&2
done
printf 'The daemon reads these settings every polling cycle, so a running daemon uses them from its next cycle without a restart.\n' >&2

plist="$HOME/Library/LaunchAgents/com.user.pr-review.plist"
if [[ -e "$plist" ]]; then
  plist_path="$(plutil -extract EnvironmentVariables.PATH raw -o - "$plist" 2>/dev/null)" || plist_path=""
  cli_dir="$(dirname "$(command -v "$cli")")"
  [[ ":$plist_path:" == *":$cli_dir:"* ]] ||
    printf 'The installed launchd job'"'"'s PATH does not include %s, where %s lives; re-run bash bin/install.sh so the background daemon can find it.\n' "$cli_dir" "$cli" >&2
fi
