# ADR 0009: Explicit polling loop instead of a launchd StartInterval timer

Date: 2026-06-07
Status: Accepted

## Context

[ADR 0001](./0001-architecture-baseline.md) D1 chose polling over webhooks and fixed the mechanism as "poll watched repositories every 5 minutes from a local `launchd` job" — implemented as a `StartInterval=300` LaunchAgent that re-runs the stateless `daemon/poll.sh` each interval (`RunAtLoad=false`; `poll.sh` "does not run its own long-lived loop").

On 2026-06-06/07 the daemon silently stopped polling for ~32h. The `StartInterval` timer stalled across a sleep/wake transition and did not resume even with the machine awake, on AC, and out of Low Power Mode: `.daemon.log` mtime frozen, `runs` counter stuck, zero `log show` spawn events, while `pmset` confirmed the system was awake the whole time. The job was loaded with `last exit code = 0`, and `gh pr list` returned the missed PR as a valid candidate throughout — the failure was entirely in the launchd driver, not the daemon code. A foreground `poll.sh` reviewed the PR correctly. The daemon emits no liveness signal, so the stall was invisible until a PR went unreviewed and was noticed by hand.

The stateless one-shot design has real virtues — crash isolation (a hung cycle cannot wedge the daemon; launchd just runs the next), no state drift, simplicity — but it makes the daemon **fully dependent on, and blind to,** launchd's interval scheduling. `StartInterval` is a best-effort, coalesced timer, not a guaranteed wall-clock cron, and is empirically fragile across sleep/wake.

## Considered options

- **Status quo (`StartInterval`)**: simplest, but the silent-stall failure mode is unacceptable for a hands-off tool, and liveness is unobservable.
- **Watchdog on the timer**: a second job kickstarts/reloads when `.daemon.log` is stale. Keeps the flaky primitive and adds a moving part; scheduling stays implicit and unobservable.
- **Explicit self-driven loop supervised by `KeepAlive` (chosen)**: a long-lived `daemon/run.sh` loops `poll.sh` with an internal `sleep`; launchd's role narrows to keeping that one process alive.
- **Move polling to an always-on host** (server / GitHub Actions cron): sidesteps laptop sleep, but collides with the Claude Code macOS-interactive assumption (ADR 0001 D2) and is larger scope; revisit as the headless / V2+ variant.

## Decision

Replace the `StartInterval` driver with an explicit long-lived loop:

- `daemon/poll.sh` stays the **single polling-cycle unit** — unchanged, still the manual one-shot and test entry point.
- New `daemon/run.sh` runs `while true; do poll.sh; sleep "$POLL_INTERVAL_SECONDS"; done` with a **pidfile singleton guard** (no double loops) and a per-cycle **heartbeat** (a timestamp the daemon writes each loop).
- The launchd template switches from `StartInterval` to **`KeepAlive` with `SuccessfulExit=false`** supervising `run.sh`: launchd keeps the process alive (restart on a non-zero exit, logout, boot) rather than firing a discrete timer. `SuccessfulExit=false` rather than bare `KeepAlive` so that when run.sh exits 0 because another loop already holds the singleton, launchd does **not** busy-respawn it every 10s; a genuine crash (non-zero) still restarts.
- Execution becomes **explicit and observable** in both modes: a terminal run is explicit foreground; `KeepAlive` / `nohup &` is explicit background. "Is it running and fresh?" is answerable from the pidfile plus heartbeat age.

This amends the **mechanism** of ADR 0001 D1 (the "via a local launchd `StartInterval` job" implementation). The polling-over-webhooks decision and the ~5-minute interval are unchanged.

## Consequences

- The silent-stall mode is removed: a crashed loop is restarted by `KeepAlive`; a wedged cycle is bounded by the existing wall-clock backstop (eee5fb8), after which the loop continues. A stale heartbeat becomes a detectable signal rather than silence.
- Crash isolation is preserved differently: previously each cycle was a fresh process; now one process loops, but the runtime backstop prevents a hung cycle from wedging it and `KeepAlive` recovers a hard crash.
- A new singleton concern: a foreground `run.sh` and a `KeepAlive` `run.sh` must not both run. The pidfile guard handles this; an operator who wants a manual one-off still uses `poll.sh` (one cycle, no loop, no pidfile).
- After wake the loop resumes within at most one interval (its `sleep` returns), instead of depending on launchd to re-fire a timer that may not.
- `StartInterval`'s incidental "serialize daemon ticks" property (noted in ADR 0008's concurrency consequence) is now provided by the single-process loop itself.
- The `poll.sh` header comment ("does not run its own long-lived loop") and the CLAUDE.md "Run daemon" section are updated; the loop moves up one level into `run.sh`.
- Backward compatibility: `bin/install.sh` / `uninstall.sh` and `templates/com.user.pr-review.plist.template` change; existing installs need one reinstall to pick up the new driver.

Tracked in #83.
