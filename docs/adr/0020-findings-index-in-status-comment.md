# ADR 0020: Findings index folded into the Status comment

Date: 2026-06-18
Status: Accepted. Relaxes ADR 0019's "Status comment = scope, never findings" boundary (the rest of ADR 0019 stands).

## Context

The daemon never aggregates a PR's findings into a cumulative picture of how many were raised and how many are now resolved. It records resolution as in-place state (ADR 0019) but still surfaces findings as stacked per-SHA review objects plus a scope-only Status comment, so that cumulative count exists only as scattered per-thread Resolution stamps.

Two symptoms, both live:

- The Status comment's headline reads `Reviewed <sha>: 0 findings`, meaning "0 *new* at this SHA." It reads as "clean PR" and hides that findings were raised and fixed (sounds-abroad#116).
- Every new HEAD SHA submits a fresh review object even when it carries no new findings, so a resolution-only or no-op push stacks an empty review (sounds-abroad#119, pull/175). #172 makes resolution-only SHAs more frequent, so each now tends to pair a real in-place resolution with a redundant empty review.

## Considered options

Host surface for the cumulative state:

- **Separate dedicated ledger comment (rejected).** A second pr-review-agent issue comment beside the Status comment. Two PR-level comments for one concept (the review's standing) is redundant surface and a second place for the operator to look.
- **A new single sticky comment merging scope and findings, CodeRabbit-style (rejected as a new artifact).** Sound in shape, but it is what the Status comment already is: a per-PR, edit-in-place comment. A new artifact would re-implement the #60 machinery (marker, find/post/edit) for no gain.
- **Expand the existing Status comment (chosen).** Broaden its content from scope to scope plus a cumulative findings index. No new comment, no new marker, no new term: the #60 machinery is reused as-is, and the Status comment is already the per-PR edit-in-place state surface.

## Decision

The Status comment carries a **findings index**: the cumulative per-finding state of the PR, edited in place each tick alongside the scope it already shows.

1. **Index, not content.** Each entry is a pointer: the finding's location, linked to its Inline comment, plus its open/resolved state. No finding body, title, severity, or type. The Inline comment stays the single source for finding content (one source per fact); the index only references it.

   The boundary forbade the Status comment holding finding *content*, which would make it a second source that drifts from the Inline comment. The index holds no such fact: it links to each finding rather than restating it, and reads each entry's open/resolved from the thread on every tick rather than storing it. It is a derived view with nothing of its own to fall out of sync, so one source per fact is preserved. The boundary's "never findings" narrows to "never finding bodies," not a hole in it.

2. **State read from GitHub, not stamps.** Each entry's open/resolved comes from the thread's `isResolved` (already returned by `fetch_open_review_threads`), the same state the merge gate enforces and the operator sees in the UI; it catches a manually-resolved thread that carries no stamp. The Resolution stamp (ADR 0019) is unchanged: it records *why* a thread closed, while *whether* it is closed is what the index reads from the thread.

3. **Counts move off the headline.** The headline drops its `: N findings` per-SHA count and becomes pure scope (`Reviewed <sha>`). All counts live in the index rollup (`N total · X open · Y resolved`, over the PR's anchored findings), so the misleading per-SHA "0 findings" line is gone.

4. **Unanchored findings stay in the Review body, noted not tracked.** A finding relocated to the Review body's `## Findings outside the diff` section has no thread, so no resolvable state and no cumulative count recoverable from threads. The index notes the current review's outside-diff count as a pointer (`+ N outside the diff → review`), never a per-item open/resolved entry it could never leave nor a cumulative figure it cannot derive. This matches the reference tool: CodeRabbit lists out-of-diff findings in the review body without per-item resolution, hitting the same constraint that a finding without a thread has no state to track.

5. **No review object on a no-new-findings tick.** When a tick surfaces zero new findings (anchored and unanchored both zero), the daemon skips the review POST. The Status comment still updates, so a resolution-only push reflects the newly-resolved threads in the index without stacking an empty review. A tick with at least one new finding posts the review as before.

## Boundary

This changes how findings are *surfaced and aggregated*, not how they are detected, judged, or resolved. Candidate selection, the per-thread judgment, the Verdict vocabulary, the Resolution stamp, and `voice.py` are unchanged. The Review object remains the authoritative surface for finding *content*; the index is a pointer to it. Where the cumulative state should live once the delivery model changes (#134; a GitHub App could open Checks API surfaces) is deferred to that decision; this ADR commits only to the issue-comment surface available today.

## Consequences

- The "0 findings" misread (sounds-abroad#116) is gone: the Status comment shows cumulative `total · open · resolved`, and the headline no longer states a per-SHA count that reads as a verdict on the whole PR.
- Empty per-SHA review objects (sounds-abroad#119, pull/175) stop: a no-new-findings tick posts no review. This pairs with #172, which makes resolution-only SHAs common.
- The index is rebuilt each tick from `fetch_open_review_threads`, which already returns every thread (open and resolved) with the fields the index needs; the query gains one field, the root comment's `url`, for the per-entry link.
- ADR 0019's "Status = scope, never findings" is relaxed to "scope plus a findings *index* (links, state, counts), never finding *bodies*" (Decision 1).
- A future delivery-model surface (#134) inherits one already-aggregated index rather than a per-SHA count to migrate.
