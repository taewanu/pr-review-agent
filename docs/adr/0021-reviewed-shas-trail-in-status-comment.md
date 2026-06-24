# ADR 0021: Reviewed-SHAs trail in the Status comment

Date: 2026-06-24

Status: Accepted. Extends ADR 0020 (findings index in the Status comment) with one piece of *owned* state alongside the comment's derived parts.

## Context

A no-new-findings tick posts no review object (ADR 0020 Decision 5), which removed the empty-review noise but left a gap: a clean HEAD SHA now leaves no durable, PR-visible record that it was reviewed. The Status comment edits in place, so its scope line, file list, and verdict are overwritten each tick; the only surviving trail belongs to findings (immutable threads plus the cumulative index). A reader of the PR cannot tell a SHA that was reviewed-and-clean from one the daemon never reached. That record lives today only in `.daemon.log`, off the PR.

The asymmetry is structural, not a bug: the Status comment is a current-state surface by design. The question is whether a per-SHA "reviewed" audit record earns a place on it, and the operator confirmed it does for the merge-time "every commit got looked at" assurance.

## Considered options

- **Re-post a review object per clean SHA (rejected).** Exactly what ADR 0020 Decision 5 removed; reintroduces the empty-review stacking (sounds-abroad#119).
- **A compact inline `Reviewed: sha · sha · …` line (B2, rejected).** Always glanceable, but carries no timestamps and competes with the scope line directly above it; the SHA list grows unbounded and would need a `last-N` cap that silently drops the early record.
- **A folded `<details>` trail (B1, chosen).** Collapsed to one summary line by default, so it adds almost no vertical weight to a populated comment; expands to per-SHA rows with the UTC reviewed-at time. Scales to a long-lived PR without crowding the current-state content above it. Cost: the record is one click away rather than glanceable, which is the right trade for an audit artifact read occasionally, not every tick.

## Decision

The Status comment carries a **reviewed-SHAs trail**: a folded `<details><summary>Reviewed N commits</summary>` block, appended below the file list and above the provenance line, listing every HEAD SHA the daemon has reviewed on this PR (newest last) with the UTC time it was reviewed.

1. **Owned state, not derived.** The trail is the one Status-comment element that *accumulates* across ticks rather than being rebuilt from current PR state. It departs from ADR 0020 Decision 1's "derived view, nothing of its own to fall out of sync": the trail is precisely a thing of its own. The findings index stays derived and unchanged; the trail is a separate, explicitly stateful element beside it.

2. **The comment body is the store.** Owned state needs a home, and the daemon has no datastore. The prior rows live in the Status comment body itself: each reviewed tick reads the existing body, parses the prior trail out of it, appends the current SHA, and re-emits the whole block. `status_trail.py` parses and renders; the body is read once per reviewed tick (`status_comment_body`, lib.sh) before the in-place edit overwrites it.

3. **Append, never overwrite.** Every other part of the Status comment is replaced each tick; the trail only grows. The pre-review "Reviewing…" render carries the prior trail unchanged (this tick's SHA is not reviewed yet); the terminal "Reviewed" render folds in the current SHA.

4. **Best-effort, like the rest of the Status comment.** A body fetch miss, a parse failure, or a render error degrades to a trail that restarts (or is omitted) rather than aborting the review that has already landed. The audit record is worth having, not worth blocking a review over.

## Boundary

This adds a record of *which SHAs were reviewed*, not what was found in them. It does not touch detection, judgment, resolution, the Verdict vocabulary, or the findings index. The trail holds no finding content; it is SHAs and timestamps. Where owned state should ultimately live once a delivery-model surface exists (#134, a GitHub App with Checks API) is deferred to that decision, the same deferral ADR 0020 made for the index.

## Consequences

- A clean SHA now leaves a durable, PR-visible record (sha + reviewed-at), closing the gap ADR 0020 Decision 5 opened.
- The Status comment gains its first owned state. A reader of `findings_index.py` ("holds no state of its own to fall out of sync") should know that claim is scoped to the index; the trail beside it is deliberately stateful (this ADR).
- One added `GET` per reviewed tick (the body read), only when a Status comment already exists. First reviews add nothing; same-SHA ticks never reach this path.
- The trail's only store is the comment body, so manual edits to that block by anyone editing the comment will be read back as the record. The provenance tag and marker keep it the daemon's comment, but the trail is not defended against hand-editing.
