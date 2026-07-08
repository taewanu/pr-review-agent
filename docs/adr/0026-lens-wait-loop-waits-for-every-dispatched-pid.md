# ADR 0026: Lens wait loop waits for every dispatched PID

Date: 2026-07-08
Status: Accepted.

## Context

Found by pr-review-agent's own self-review of the PR that introduced it (taewanu/pr-review-agent#192, ADR 0023's multi-lens generation), reviewed as `important`/`bug`:

`review-pr.sh`'s wait loop iterated `lens_pids` in dispatch order and called `exit 1` the moment one lens timed out or produced empty output, without waiting on or killing the other already-backgrounded lenses still running. With `CLAUDE_SLOT_POOL_SIZE` smaller than the lens count (the bash fallback default is 3, for 5 lenses), the later-dispatched lenses acquire their slot and start their own `REVIEW_AGENT_TIMEOUT` clock later in wall time, so an earlier lens finishing (successfully or not) well before a later one is the normal case, not a rare race. The immediate `exit 1` fired the script's `EXIT` trap (`flip_status_failed; cleanup`), and `cleanup()` unconditionally `rm -rf`s `$SCRATCH`, the same directory the still-running sibling lenses were reading from and writing their raw output to. The still-running subshells became orphaned background jobs: still burning a `claude -p` call (real cost) against a scratch clone that could be deleted out from under them mid-write, for a result nothing would ever read.

## Decision

The wait loop now waits on every dispatched PID unconditionally, in dispatch order, regardless of any earlier lens's outcome. A lens that timed out or produced no output is logged (`log_info`, not `log_failure`) and the loop continues to the next PID; it is no longer treated as a reason to abort the whole review. This is safe because `merge_findings.merge()` already tolerates one lens's payload failing to parse without discarding the others' (ADR 0024): an empty or missing raw file for one lens now flows into the same per-lens skip path a malformed payload already used, rather than a new code path having to be invented for it.

## Boundary

This does not change what happens when every lens fails (`merge_findings.py`'s own `all-lenses-failed` category, ADR 0024, still fires and the review fails as before); it only stops one lens's timeout or empty output from preempting the wait for the others. It does not add active cancellation of a still-running lens once another has failed: killing the rest was considered and rejected, since a slow-but-eventually-successful lens is exactly the "not a rare edge case" scenario this ADR is about, and killing it would trade a real, cheap `claude -p` result for nothing.

## Consequences

- A single slow or timed-out lens no longer discards the other lenses' completed, valid work, nor deletes the scratch clone out from under them while they are still writing to it.
- One fewer orphaned-process/wasted-cost failure mode under the shared slot pool (ADR 0023) when `CLAUDE_SLOT_POOL_SIZE` is smaller than the lens count, which is the common case, not a misconfiguration.
- The per-lens `<label>-review-timeout` / `<label>-empty-stdout` failure categories (ADR 0005/0023) are no longer raised from this loop; a lens hiccup is now diagnosable via the `log_info` line rather than a system failure category, since it no longer fails the review on its own.
