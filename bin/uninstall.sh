#!/usr/bin/env bash
# uninstall.sh — boot out the launchd job and remove the plist. Leaves state
# files at ~/.pr-review-agent/ untouched so reinstall picks up
# where it left off.

set -euo pipefail

PLIST_NAME="com.user.pr-review"
PLIST_DST="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

if [[ ! -r "$PLIST_DST" ]]; then
  echo "No plist at $PLIST_DST — already uninstalled"
  exit 0
fi

# Modern launchctl bootout (deprecated `unload` was unreliable, #83). Sends
# SIGTERM to the run.sh loop, which clears its pidfile via the cleanup trap.
launchctl bootout "gui/$(id -u)/${PLIST_NAME}" 2>/dev/null || true
rm "$PLIST_DST"
echo "Uninstalled $PLIST_DST"
