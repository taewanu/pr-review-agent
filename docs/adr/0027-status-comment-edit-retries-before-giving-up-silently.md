# ADR 0027: Status comment edit retries before giving up silently

Date: 2026-07-08
Status: Accepted.

## Context

`taewanu/sounds-abroad#192`'s status comment stayed on `👀 Reviewing <sha>…` long after the daemon had actually finished reviewing that SHA (zero findings, so no review object was posted, per ADR 0020): the local state file recorded the SHA as reviewed, so the daemon never touched it again, yet the comment never advanced to its terminal `✅ Reviewed` render.

`edit_status_comment` (`daemon/lib.sh`) was a single `gh api -X PATCH` call with `|| true`: any failure, including a transient one, was swallowed completely, with nothing written to `.daemon.log`. The operator's account of the incident (a laptop sleep/resume cycle mid-review) matches a known real failure mode this session's own dogfood logs already showed repeatedly (`read: connection reset by peer`, `operation timed out`): a `gh api` call failing for a few seconds right as the network comes back. Every other part of the pipeline recovers from a transient `gh api` failure via the *next polling cycle* retrying the whole review; this one write does not fit that pattern, since a completed review's SHA is never revisited, so a failure on this specific call has no other path back to a correct state.

## Decision

`edit_status_comment` retries up to 3 times, with a `sleep` between attempts (`STATUS_EDIT_RETRY_SLEEP_SECONDS`, default 2s, overridable so tests run at 0). It still always returns 0 and never aborts a landed review: that trade-off (a flaky status edit is not worth failing a real review over) stands. What changes is that exhausting all 3 attempts now logs `status comment edit failed after 3 attempts (comment <id>)` via `log_info`, so a future occurrence is at least visible in `.daemon.log` instead of only discoverable by an operator noticing a stale comment on GitHub by eye.

## Consequences

- A transient network blip around this one call (laptop resume, brief `gh` outage) is now absorbed rather than permanently freezing a PR's status comment on a stale render.
- A failure that outlives 3 quick attempts is still swallowed as far as the review's own exit code goes, but is now logged, closing the diagnosability gap that made this incident take a manual `gh api` investigation to even explain.
- `sounds-abroad#192`'s specific stuck comment predates this fix and is not self-healing (the daemon believes that SHA is done); it needs a one-off manual `gh api` edit to correct, independent of this ADR.
