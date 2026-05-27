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
poll_interval="$(grep -E '^POLL_INTERVAL_SECONDS=' "$REPO_ROOT/.env" | head -1 | cut -d= -f2- | tr -d '"'\''')"
poll_interval="${poll_interval:-300}"
if ! [[ "$poll_interval" =~ ^[0-9]+$ ]] || [[ "$poll_interval" -lt 1 ]]; then
  echo "ERROR: POLL_INTERVAL_SECONDS in .env is not a positive integer ($poll_interval)" >&2
  exit 1
fi

# Idempotent reinstall — unload existing job if any. `launchctl list` exits 0
# when the label exists.
if launchctl list "$PLIST_NAME" >/dev/null 2>&1; then
  echo "Unloading existing $PLIST_NAME..."
  launchctl unload "$PLIST_DST" 2>/dev/null || true
fi

mkdir -p "$HOME/Library/LaunchAgents"

# Substitute the four placeholders. `|` as sed delimiter avoids escaping slashes
# in $PATH / $HOME / $REPO_ROOT.
sed \
  -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
  -e "s|__POLL_INTERVAL_SECONDS__|${poll_interval}|g" \
  -e "s|__PATH__|${PATH}|g" \
  -e "s|__HOME__|${HOME}|g" \
  "$PLIST_SRC" >"$PLIST_DST"

launchctl load "$PLIST_DST"

echo "Installed $PLIST_DST"
echo "  Repo:     $REPO_ROOT"
echo "  Interval: ${poll_interval}s"
echo "  Logs:     $REPO_ROOT/.daemon.log"
echo ""
echo "Tail logs: tail -f $REPO_ROOT/.daemon.log"
echo "Stop:      bash $SCRIPT_DIR/uninstall.sh"
