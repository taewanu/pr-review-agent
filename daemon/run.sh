#!/usr/bin/env bash
# run.sh — the polling loop (ADR 0009). Drives poll.sh on a fixed interval as a
# long-lived process, replacing the launchd StartInterval timer that stalled
# silently across sleep/wake (#83). launchd's role narrows to KeepAlive —
# supervising this one process — or an operator runs it directly in a terminal.
# A pidfile singleton stops two loops from both driving ticks; a per-cycle
# heartbeat makes liveness observable (compare its epoch against `date +%s`).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=daemon/lib.sh disable=SC1091
source "$SCRIPT_DIR/lib.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Interval resolution: an explicit POLL_INTERVAL_SECONDS in the environment (the
# operator's shell for a foreground run) wins; otherwise read it from .env; else
# default 300. resolve_tunable does the env-then-.env lookup with the safe grep
# parse and is pipefail-clean on an absent key.
poll_interval="$(resolve_tunable POLL_INTERVAL_SECONDS "$REPO_ROOT/.env")"
poll_interval="${poll_interval:-300}"
if ! [[ "$poll_interval" =~ ^[0-9]+$ ]] || [[ "$poll_interval" -lt 1 ]]; then
  log_err "POLL_INTERVAL_SECONDS is not a positive integer ($poll_interval)"
  exit 1
fi

# Singleton: refuse to start a second loop. A dead holder's pidfile is reclaimed,
# so this only blocks a genuinely live overlap — e.g. a manual foreground run.sh
# while the launchd one is up. Exit 0 (not an error): the existing loop is fine.
# Registered BEFORE the cleanup trap so this exit never removes the live holder's
# pidfile.
if ! PIDFILE="$(acquire_daemon_singleton)"; then
  log_err "another pr-review loop is already running (pidfile $(_daemon_pid_path)) — exiting"
  exit 0
fi
# Release the singleton on exit so KeepAlive (or a manual restart) starts clean.
cleanup() { release_daemon_singleton "$PIDFILE"; }
trap cleanup EXIT
trap 'exit 0' INT TERM

# Boot-time, not per-tick: poll.sh runs every cycle, and repeating a static
# drift warning each cycle would bury it (#201).
warn_env_drift "$REPO_ROOT/.env" "$REPO_ROOT/templates/.env.example"

# Name any watched repo the App is not installed on, once at boot (ADR 0036
# decision 4). poll.sh skips such a repo every cycle; surfacing the permanent
# condition here keeps that skip from repeating, the same reason the drift
# warning is boot-time. Best-effort: discover_missing_installations logs and
# returns cleanly on a probe failure, so a transient network error never turns a
# KeepAlive restart into a loop. REPOS is space-separated, so the split is
# intended.
app_id="$(resolve_tunable GITHUB_APP_ID "$REPO_ROOT/.env")"
repos_raw="$(resolve_tunable REPOS "$REPO_ROOT/.env")"
if [[ -n "$app_id" && -n "$repos_raw" ]]; then
  # shellcheck disable=SC2086  # deliberate word-split over the space-separated list
  discover_missing_installations "$app_id" $repos_raw >/dev/null || true
fi

log_info "polling loop up (pid $$, interval ${poll_interval}s, driver=run.sh per ADR 0009)"
while true; do
  write_heartbeat || log_err "heartbeat write failed"
  bash "$SCRIPT_DIR/poll.sh" || log_err "poll cycle exited non-zero — continuing"
  sleep "$poll_interval"
done
