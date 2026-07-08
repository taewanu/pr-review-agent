# ADR 0024: Status-comment sentinel for zero-finding dedup

Date: 2026-07-06
Status: Accepted. Closes a gap ADR 0020 opened in ADR 0006's invariant.

## Context

ADR 0006's dedup is sentinel-first: `discover_sentinel_sha` reads the operator's own PR reviews and issue comments, takes the newest by timestamp, and extracts a `<!-- pr-review-agent:sha:X -->` marker `create-review.sh` embeds when it submits a review object. Only when no sentinel is found anywhere does `poll.sh` fall back to its local per-PR state file.

ADR 0020 later added: when a tick surfaces zero new findings, the daemon skips the review-object POST entirely (avoiding an empty review stacking on every no-op push). That review object was the only place a sentinel got embedded. So a zero-finding tick completes successfully, `poll.sh`'s `state_write` correctly records the reviewed SHA in the state file, but no sentinel gets embedded anywhere `discover_sentinel_sha` looks.

The next tick's `discover_sentinel_sha` still finds a sentinel: the stale one from whichever review last did have findings. It returns that (rc 0, "success"), so `poll.sh` never falls through to the correct state file at all: sentinel-first means a stale sentinel outranks a fresh state file unconditionally. The daemon concludes the PR wasn't reviewed at this HEAD and reviews it again, in full. If the PR has since stabilized (no more new findings), this repeats every tick, forever, until a new commit lands or a fresh finding appears: real `claude -p` cost for a review whose answer never changes.

Confirmed live: sounds-abroad#185 re-reviewed the identical HEAD SHA across two consecutive polling cycles, at $3.14 for the second (wasted) pass, while sounds-abroad#133 (an unrelated, genuinely unchanged PR) correctly skipped via the same-SHA check both times.

## Considered options

- **Flip poll.sh's priority to freshest-of-sentinel-or-state (rejected).** Would need every consulted timestamp normalized and compared across two independent sources (GitHub's review/comment API clock vs. the local state file's own write time), and changes the actual dedup *decision* logic ADR 0006 established and tests pin. More moving parts for the same outcome as the option below.
- **Have `create-review.sh` post a minimal marker-only comment on a zero-finding tick (rejected).** Restores the invariant but reintroduces exactly the empty-comment stacking ADR 0020 was written to stop, just renamed from "review" to "comment."
- **Embed the sentinel in the Status comment (chosen).** The Status comment already updates every tick regardless of findings (ADR 0020's own headline swap: `Reviewed <sha>`, edited in place, never skipped). `discover_sentinel_sha` already scans issue comments as a source (originally for the #49 body-wipe case). No new artifact, no new API surface, and the fix is additive: one more hidden HTML comment in a body that already carries the Status marker.

## Decision

1. **`render_status_comment` takes an optional 7th arg, `sentinel-sha`.** When given, it embeds `<!-- pr-review-agent:sha:X -->` (same format `create-review.sh` uses) between the Provenance tag and the Status marker. Only `review-pr.sh`'s terminal `"✅ Reviewed"` render passes it (`$HEAD_OID`, after the review has actually completed). The pre-review `"👀 Reviewing…"` render and the failure render never pass it: embedding a sentinel there would claim a review that has not finished (or did not finish), and a subsequent crash or timeout would then have the next tick wrongly skip retrying it. This is a stronger risk than the bug being fixed, so the omission is deliberate on both of those paths.

2. **`discover_sentinel_sha`'s comment timestamp switches from `created_at` to `updated_at` (falling back to `created_at`)**. The Status comment is created once, on first review, and edited (not recreated) every tick after. Its `created_at` is fixed at that first-review moment forever; sorting on it would always rank the Status comment's freshly re-embedded sentinel as older than any later-submitted review, silently defeating Decision 1. `updated_at` reflects the last edit, confirmed against a live status comment (`created_at` 2026-07-03, `updated_at` 2026-07-06 after several same-day edits). Reviews keep `submitted_at // created_at`: a submitted review is not edited in place the same way, so this side was not broken.

## Boundary

This does not change which SHA counts as "reviewed" or how the merge-object skip (ADR 0020) decides to post; it only makes a zero-finding tick's completion discoverable the same way a findings-carrying tick's already was. The state-file fallback (ADR 0006) is untouched and still exists for the case `discover_sentinel_sha` finds nothing anywhere.

## Consequences

- A PR that stabilizes to zero new findings is reviewed once more after the last real finding, then correctly skipped every tick after, rather than forever.
- The Status comment now carries two hidden HTML comments (Status marker, sentinel) instead of one; both are invisible in the rendered comment.
- `discover_sentinel_sha`'s comments query now needs `updated_at` in its `--json`/API response, already present on the standard issue-comments payload with no additional field request.
