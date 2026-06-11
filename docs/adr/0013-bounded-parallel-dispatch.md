# ADR 0013: Bounded-parallel PR review dispatch

Date: 2026-06-11
Status: Accepted

## Context

[ADR 0009](./0009-explicit-polling-loop.md) makes `daemon/poll.sh` a single polling cycle that `run.sh` loops. Within a cycle, `poll.sh` iterates open PRs and dispatches `review-pr.sh` one at a time, so a cycle's wall-clock is the sum of every per-PR review. A review runs a multi-minute `claude -p`; with several active PRs, a slow one delays every PR behind it, and the next cycle cannot start until the whole batch drains.

The binding constraint on running reviews concurrently is concurrent `claude -p` load (subscription and rate limits, plus local machine load), not GitHub API throughput. So the goal is a small, bounded fan-out, not unbounded parallelism.

The data-safety substrate for parallel reviews of distinct PRs is already in place: each `review-pr.sh` makes its own `mktemp -d` scratch and removes it on exit; a per-PR lock (#67) prevents two reviews of the same PR from overlapping; and `state_write` is an atomic per-PR `mktemp` + `mv` keyed by PR. Distinct-PR reviews share no mutable state.

The runtime is stock macOS `bash` 3.2 (mise does not pin bash), which has no `wait -n` (wait-for-any).

## Considered options

- **Serial status quo**: simplest, but cycle latency grows linearly with the active-PR count and one slow review stalls the rest.
- **`xargs -P N`**: offloads the bound, but the per-PR dispatch decision (sentinel discovery, state fallback, the ADR 0006 rc=2 skip, `--last-sha`) is non-trivial bash that would have to move into a child shell or helper script, fighting the existing loop structure.
- **`wait -n` semaphore**: the clean wait-for-any pattern, but unavailable on bash 3.2; would force a hard dependency on a newer bash the operator may not have.
- **Head-index FIFO PID semaphore (chosen)**: keep the dedup loop serial and foreground; background only the heavy `review-pr.sh` leg. Track background PIDs in an array; when the in-flight count reaches the cap, block on the oldest PID to free a slot. A plain integer head index avoids array slicing, so it is safe under `set -u` on bash 3.2.

## Decision

Add a `MAX_PARALLEL` config knob (default `1`) and bound the review fan-out to it.

- `load-config.py` gains `max_parallel: int = 1`, parsed from the `MAX_PARALLEL` env and validated `>= 1`; it flows to `poll.sh` through the existing config JSON.
- `poll.sh` keeps eligibility skips, reply dispatch, and dedup serial and foreground. Only the review tail (`review-pr.sh` plus its own state write) is backgrounded, capped by a head-index FIFO semaphore. The cap is **global across repos**, since the constraint is total concurrent `claude -p` load, not per-repo.
- Each backgrounded review owns its outcome: it writes per-PR state only on success and logs a failure otherwise, so a failed review never advances state and never aborts the cycle. The cycle drains all in-flight reviews before reporting done.
- `MAX_PARALLEL=1` reproduces ADR 0009's serial behavior exactly (dispatch one, wait, dispatch the next), so parallelism is strictly opt-in.

No hard upper bound is enforced. The recommended ceiling is small (2 to 3): the limiter is concurrent `claude -p`, and a many-PR cycle at a high cap can trip subscription rate limits.

## Consequences

- A cycle's wall-clock drops from the sum of per-PR reviews toward `ceil(N / MAX_PARALLEL)` times the per-review cost; a single slow review no longer blocks the others.
- The bound is FIFO (block on the oldest in-flight review), not wait-for-any, because bash 3.2 lacks `wait -n`. A fast newer review cannot free the slot the oldest still holds, so utilization is marginally below an ideal wait-any scheduler. For minute-scale reviews where order does not matter this is negligible, and it keeps the daemon runnable on stock macOS bash.
- The within-cycle serialization that ADR 0009 inherited (and [ADR 0008](./0008-own-pr-auto-submit.md) leaned on after dropping the pending-review lock) is now relaxed for **distinct** PRs. Same-PR serialization still holds via the per-PR lock (#67), so the own-PR auto-submit double-post window ADR 0008 describes does not reopen.
- Logs from concurrent reviews interleave in `.daemon.log`; each line stays prefixed, but a single review's lines are no longer contiguous.
- At a high `MAX_PARALLEL` on a busy cycle, concurrent `claude -p` processes can hit rate limits. Default `1` and the documented conservative ceiling keep this opt-in and visible.
- Out of scope: cross-cycle scheduling, per-PR priority, and any global rate-limit backoff. This ADR bounds within-cycle fan-out only.

Tracked in #92.
