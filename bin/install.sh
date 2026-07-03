#!/usr/bin/env bash
# install.sh — register the launchd job for poll.sh. Re-runnable; reinstall
# replaces the existing plist and reloads.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PLIST_NAME="com.user.pr-review"
PLIST_SRC="$REPO_ROOT/templates/com.user.pr-review.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

if [[ ! -r "$PLIST_SRC" ]]; then
  echo "ERROR: plist template missing at $PLIST_SRC" >&2
  exit 1
fi
if [[ ! -r "$REPO_ROOT/.env" ]]; then
  echo "ERROR: .env not found at $REPO_ROOT/.env" >&2
  echo "       Copy templates/.env.example to .env and edit, then re-run." >&2
  exit 1
fi
if ! command -v launchctl >/dev/null 2>&1; then
  echo "ERROR: launchctl not found — install.sh is macOS-only (V1 scope)" >&2
  exit 1
fi

# Read POLL_INTERVAL_SECONDS from .env, default 300. The bash subset of .env we
# accept doesn't need full `source` — a simple grep is safer (no surprise eval).
# Brace-guard the grep so a missing key (exit 1) doesn't abort under pipefail.
# This script doesn't source lib.sh, so it can't reuse resolve_tunable.
poll_interval="$({ grep -E '^POLL_INTERVAL_SECONDS=' "$REPO_ROOT/.env" || true; } | head -1 | cut -d= -f2- | tr -d '"'\''')"
poll_interval="${poll_interval:-300}"
if ! [[ "$poll_interval" =~ ^[0-9]+$ ]] || [[ "$poll_interval" -lt 1 ]]; then
  echo "ERROR: POLL_INTERVAL_SECONDS in .env is not a positive integer ($poll_interval)" >&2
  exit 1
fi

# Idempotent reinstall — boot out any existing job first. Modern launchctl:
# `load`/`unload` are deprecated and were observed not to arm the job reliably
# (#83), so install/uninstall use bootstrap/bootout.
DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$PLIST_NAME" 2>/dev/null || true

mkdir -p "$HOME/Library/LaunchAgents"

# Substitute the three placeholders. `|` as sed delimiter avoids escaping slashes
# in $PATH / $HOME / $REPO_ROOT. The interval is no longer baked into the plist —
# run.sh reads POLL_INTERVAL_SECONDS from .env at loop time (ADR 0009).
sed \
  -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
  -e "s|__PATH__|${PATH}|g" \
  -e "s|__HOME__|${HOME}|g" \
  "$PLIST_SRC" >"$PLIST_DST"

launchctl bootstrap "$DOMAIN" "$PLIST_DST"
launchctl kickstart "$DOMAIN/$PLIST_NAME"

echo "Installed $PLIST_DST"
echo "  Repo:     $REPO_ROOT"
echo "  Interval: ${poll_interval}s"
echo "  Logs:     $REPO_ROOT/.daemon.log"
echo ""
echo "Tail logs: tail -f $REPO_ROOT/.daemon.log"
echo "Stop:      bash $SCRIPT_DIR/uninstall.sh"
