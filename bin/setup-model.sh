#!/usr/bin/env bash
# setup-model.sh: choose the agent CLI, model, and effort the review agents run
# on, check them, and write REVIEW_HARNESS, REVIEW_MODEL, and REVIEW_EFFORT to
# .env (or to the path given). Re-runnable; .env stays untouched unless every
# step passes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=daemon/lib.sh disable=SC1091
source "$REPO_ROOT/daemon/lib.sh"

DOTENV="${1:-$REPO_ROOT/.env}"
WORK=""
OUT=""
cleanup() { rm -rf "$WORK" "$OUT"; }
trap cleanup EXIT

fail() {
  log_err "$*"
  exit 1
}

# ask <prompt> <default> [shown-default]: prints the answer, or the default on an
# empty one. Prompts go to stderr because callers capture stdout.
ask() {
  local answer
  printf '%s [%s]: ' "$1" "${3:-$2}" >&2
  if ! IFS= read -r answer; then
    printf '\n' >&2
    fail "no answer on input; nothing was written"
  fi
  printf '%s' "${answer:-$2}"
}

# choose <prompt> <default> <allowed, space-separated>: asks until the answer is allowed.
choose() {
  local answer
  while true; do
    answer="$(ask "$1" "$2")" || exit 1
    [[ " $3 " == *" $answer "* ]] && break
    printf 'Choose one of: %s\n' "$3" >&2
  done
  printf '%s' "$answer"
}

for cmd in jq python3; do
  command -v "$cmd" >/dev/null 2>&1 || fail "$cmd is not on PATH; install it, then re-run bash bin/setup-model.sh"
done

current_harness="$(resolve_review_harness "$DOTENV" 2>/dev/null)" || current_harness=claude
cli="$(choose "Agent CLI to review with, claude or codex" "$current_harness" "claude codex")"

case "$cli" in
  claude) product="Claude Code" ;;
  codex) product="the Codex CLI" ;;
esac
command -v "$cli" >/dev/null 2>&1 || fail "$cli is not on PATH; install ${product}, then re-run bash bin/setup-model.sh"
if [[ "$cli" == claude ]]; then
  require_claude_version || exit 1
  # The status JSON names the account, so only its loggedIn field is read.
  claude auth status --json 2>/dev/null | jq -e .loggedIn >/dev/null 2>&1 ||
    fail "Claude Code is not logged in; run claude auth login"
else
  require_real_codex || exit 1
  codex_bin="$(python3 -c 'import os, sys
sys.path.insert(0, sys.argv[1])
import codex_exec
print(codex_exec.find_codex(os.environ.get("PATH", os.defpath))[0])' "$REPO_ROOT/daemon")"
  "$codex_bin" login status >/dev/null 2>&1 || fail "Codex is not logged in; run codex login"
fi

current_model=""
[[ "$current_harness" != "$cli" ]] || current_model="$(resolve_tunable REVIEW_MODEL "$DOTENV")"
catalog=""
if [[ "$cli" == codex ]]; then
  slugs=()
  # Without --bundled the catalog is the account's own list, not the binary's.
  if catalog="$("$codex_bin" debug models 2>/dev/null)"; then
    printf 'Models your codex account lists:\n' >&2
    while IFS=$'\t' read -r slug description; do
      slugs+=("$slug")
      printf '  %d) %s  %s\n' "${#slugs[@]}" "$slug" "$description" >&2
    done < <(jq -r '[.models[]? | select(.visibility == "list")] | sort_by(.priority)[]
      | [.slug, (.description // "")] | @tsv' <<<"$catalog" 2>/dev/null || true)
  fi
  if ((${#slugs[@]} == 0)); then
    catalog=""
    printf 'codex debug models listed no models, so type a model name the codex CLI accepts.\n' >&2
    model="$(ask "Model" "$current_model")"
  else
    printf '  %d) type a model name\n' "$((${#slugs[@]} + 1))" >&2
    model="$(ask "Model, a number or a name" "${current_model:-1}")"
    if [[ "$model" =~ ^[0-9]+$ ]]; then
      pick=$((10#$model))
      if ((pick >= 1 && pick <= ${#slugs[@]})); then
        model="${slugs[$((pick - 1))]}"
      elif ((pick == ${#slugs[@]} + 1)); then
        model="$(ask "Model name" "$current_model")"
      else
        fail "there is no model numbered ${model}; nothing was written"
      fi
    fi
  fi
  REVIEW_MODEL="$model" resolve_review_model "$DOTENV" codex >/dev/null || exit 1
else
  default_model="${current_model:-$(REVIEW_MODEL='' resolve_review_model /dev/null)}"
  printf 'Model the claude CLI runs; the default is %s. From claude --help:\n' "$default_model" >&2
  claude --help 2>/dev/null | awk '/^ +--model /{on=1; print; next} on && /^   /{print; next} {on=0}' >&2 || true
  model="$(ask "Model" "$default_model")"
  [[ "$model" == *claude-* ]] ||
    printf '%s is an alias, so the model it names can change when the claude CLI updates.\n' "$model" >&2
fi

levels="$REVIEW_EFFORT_LEVELS"
if [[ -n "$catalog" ]]; then
  supported="$(jq -r --arg m "$model" 'first(.models[]? | select(.slug == $m)
    | [.supported_reasoning_levels[]?.effort] | join(" ")) // ""' <<<"$catalog")"
  if [[ -n "$supported" ]]; then
    levels=""
    for level in $REVIEW_EFFORT_LEVELS; do
      [[ " $supported " != *" $level "* ]] || levels="${levels:+$levels }$level"
    done
    [[ -n "$levels" ]] || fail "$model supports none of the effort levels the daemon accepts ($REVIEW_EFFORT_LEVELS); nothing was written"
  fi
fi
default_effort="$(resolve_review_effort "$DOTENV" 2>/dev/null)"
[[ " $levels " == *" $default_effort "* ]] || default_effort="${levels%% *}"
effort="$(choose "Effort, one of: $levels" "$default_effort" "$levels")"

printf '\n' >&2
if [[ "$cli" == codex ]]; then
  printf '%s\n' 'On a personal ChatGPT plan, content sent through Codex, including code from each per-PR clone, can be used for training by default. Check ChatGPT Settings, Data controls, "Improve the model for everyone" and Codex'"'"'s "Include environments" before reviewing private repositories.' >&2
else
  printf '%s\n' 'On a Claude consumer plan, whether your sessions can be used for training is set at claude.ai/settings/data-privacy-controls; check it before reviewing private repositories.' >&2
fi
printf '\n' >&2

answer="$(ask "Send one test call (the prompt \"Reply with OK\") to ${model} on ${cli}?" Y Y/n)"
case "$answer" in
  [Yy]*)
    REVIEW_HARNESS="$cli" REVIEW_MODEL="$model" REVIEW_EFFORT="$effort"
    # The agent runs in an empty directory, so the call carries no code.
    WORK="$(mktemp -d -t pr-review-setup.XXXXXX)"
    OUT="$(mktemp -d -t pr-review-setup-out.XXXXXX)"
    printf 'Reply with OK\n' >"$OUT/prompt.txt"
    rc=0
    (cd "$WORK" && run_agent read-only 120 "$OUT/prompt.txt" "$OUT/raw.txt" setup) || rc=$?
    if ((rc != 0)) || [[ ! -s "$OUT/raw.txt" ]]; then
      log_err "${model} did not answer on ${cli}; nothing was written. Its last stderr lines:"
      tail -n 5 "$OUT/raw.txt.stderr" >&2 2>/dev/null || true
      exit 1
    fi
    printf '%s answered on %s.\n' "$model" "$cli" >&2
    ;;
  *) printf '%s on %s is not verified; the first review will show whether it works.\n' "$model" "$cli" >&2 ;;
esac

set_dotenv_keys "$DOTENV" "REVIEW_HARNESS=$cli" "REVIEW_MODEL=$model" "REVIEW_EFFORT=$effort" || exit 1
printf 'Wrote to %s:\n' "$DOTENV"
printf 'REVIEW_HARNESS=%s\nREVIEW_MODEL=%s\nREVIEW_EFFORT=%s\n' "$cli" "$model" "$effort"
for key in REVIEW_HARNESS REVIEW_MODEL REVIEW_EFFORT; do
  [[ -z "$(printenv "$key" || true)" ]] ||
    printf '%s is also set in your shell environment, which overrides .env.\n' "$key"
done
printf 'The daemon reads these settings every polling cycle, so a running daemon uses them from its next cycle without a restart.\n'

plist="$HOME/Library/LaunchAgents/com.user.pr-review.plist"
if [[ -e "$plist" ]]; then
  plist_path="$(plutil -extract EnvironmentVariables.PATH raw -o - "$plist" 2>/dev/null)" || plist_path=""
  cli_dir="$(dirname "$(command -v "$cli")")"
  [[ ":$plist_path:" == *":$cli_dir:"* ]] ||
    printf 'The installed launchd job'"'"'s PATH does not include %s, where %s lives; re-run bash bin/install.sh so the background daemon can find it.\n' "$cli_dir" "$cli"
fi
