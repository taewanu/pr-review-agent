#!/usr/bin/env bash
# uninstall.sh — unload the launchd job and remove the plist. Leaves state
# files at ~/.local/state/pr-review-agent/ untouched so reinstall picks up
# where it left off.

set -euo pipefail

PLIST_NAME="com.user.pr-review"
PLIST_DST="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

if [[ ! -r "$PLIST_DST" ]]; then
  echo "No plist at $PLIST_DST — already uninstalled"
  exit 0
fi

launchctl unload "$PLIST_DST" 2>/dev/null || true
rm "$PLIST_DST"
echo "Uninstalled $PLIST_DST"
