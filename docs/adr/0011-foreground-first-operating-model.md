# ADR 0011: Foreground-first operating model; launchd background is optional

Date: 2026-06-08
Status: Accepted

## Context

[ADR 0009](./0009-explicit-polling-loop.md) replaced the launchd `StartInterval` timer with an explicit `run.sh` polling loop supervised by a launchd `KeepAlive` job. The docs framed that launchd install as the primary way to run the daemon, with a foreground `bash daemon/run.sh` mentioned only as "equally valid."

That framing produced a silent failure. The daemon was installed from a working checkout that doubled as a development tree. A branch checkout in that tree (a concurrent session moving `HEAD` to an old commit) regressed the on-disk daemon code, while the launchd-supervised loop kept running from its in-memory state — so it logged `polling…` each cycle but no longer reviewed anything. Because it was a background launchd process, nothing surfaced the breakage; it was found only by manual diagnosis.

The root cause is visibility, not the loop mechanism — ADR 0009 stands. A foreground run in a terminal makes the same breakage obvious: the operator sees the process, is far less likely to branch-switch the tree under it, and notices immediately if it stops behaving.

## Considered options

- **Keep launchd-primary, add tree-state guards**: still an invisible process; the race window between "tree changed" and "operator notices" remains.
- **Run the daemon from a dedicated second clone**: robust, but forces every operator to maintain two clones for a single-user tool — friction the primary use case should not carry.
- **Foreground-first, launchd optional (chosen)**: the default documented path is a visible terminal process; launchd stays for operators who want always-on / reboot-surviving operation.

## Decision

- The primary, documented way to run the daemon is **foreground**: `bash daemon/run.sh`. Progress prints to the terminal (`log_info` / `log_err` write to stderr; the `.daemon.log` redirect is a launchd-only artifact, not inherent to the loop). Ctrl-C stops it cleanly — the `INT` / `TERM` trap releases the pidfile singleton. Updating is Ctrl-C → `git pull` → re-run. Because `run.sh` re-invokes `bash poll.sh` each cycle, a pull that only touches `poll.sh` or the review pipeline is picked up on the next tick without a restart; only a change to `run.sh`'s own loop body requires one.
- The launchd `KeepAlive` install (`bin/install.sh`, ADR 0009) remains an **optional** path for always-on operation that survives logout and reboot. Its tradeoff is documented: it is invisible and bound to its checkout's working tree, so that checkout must stay on `main` (or be a clone dedicated to running the daemon). Switching its branch silently breaks the running daemon — the failure that motivated this ADR.

## Consequences

- README and CLAUDE.md lead with the foreground command; `bin/install.sh` is presented as "run it in the background (optional)."
- The mechanism from ADR 0009 (the `run.sh` loop, pidfile singleton, per-cycle heartbeat) is unchanged. Only the recommended way to invoke it changes, and the failure mode of the background install is now stated where the install is.
- An operator who both develops the tool and dogfoods the daemon runs the dogfood daemon in the foreground (visible), or from a clone separate from the branch-switching dev tree. The "keep it on `main`" rule is no longer an unstated invariant; it lives next to the optional install.
