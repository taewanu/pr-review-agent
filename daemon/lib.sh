# shellcheck shell=bash
# lib.sh — shared helpers sourced by other daemon scripts. Not executable on its own.
#
# Slice 1 scope: stderr logging only. Failure categorization (ADR 0005) lands in Slice 4.

log_info() {
  printf '[pr-review-agent] %s\n' "$*" >&2
}

log_err() {
  printf '[pr-review-agent] ERROR: %s\n' "$*" >&2
}
