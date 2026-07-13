# ADR 0033: Per-push delta in the Status comment

Date: 2026-07-13
Status: Accepted. Extends the ADR 0020 (findings index) → ADR 0021 (reviewed-SHAs trail) Status-comment arc with a fourth derived view.

## Context

The Status comment shows a PR's cumulative finding state (the index's `total · open · resolved` rollup, ADR 0020) and the SHAs reviewed (the trail, ADR 0021), but never states what a single push *changed*. An author who pushes a fix reads the same rollup as before and re-derives "did this commit help?" by eye, comparing the open count against what they remember from the last tick.

react.doctor's sticky PR summary names this directly ("what your change added or fixed"), and the framing is consistent with a stance the taxonomy already takes: `pre_existing` separates nearby-unchanged issues from what this PR introduced (ADR 0002), so "what *this change* did" is already a distinction the review draws. The teardown that surfaced this (memory `project_react_doctor_reference`) also considered a rolled-up 0-100 health score, react.doctor's other summary element, and dropped it: the index rollup already answers "how healthy" deterministically, so a score would layer fiat weights and per-run stochastic jitter over a signal already shown, for little new information.

## Considered options

Source of the `new` and `fixed` counts:

- **Diff the prior Status comment body against the current one (rejected).** Parse the prior index's entries back out of the comment body (as the trail already round-trips its rows), diff against this tick's index to derive new and newly-resolved. It works, but it re-derives from a rendered surface what the pipeline already knows first-hand, and it inherits every parse fragility the trail regex carries. It also muddies "new": an index entry present now but absent in the prior body could be genuinely new or could be a finding that flickered out and back under review variance, and the body-diff cannot tell them apart.
- **Read the counts from the tick's own actions (chosen).** This tick *posts* its new findings and *resolves* the threads its fix-check judged fixed. Both counts already exist as pipeline state: `new_findings_total` (anchored + unanchored posted this tick) and the length of the commit-driven resolution's stamps file. The delta is then literally what this push did, not an inference about what changed between two rendered comments.

## Decision

The Status comment carries a per-push **delta line** on a re-review: a one-line summary of what the current tick added and resolved, derived from the tick's own posts and resolves.

1. **Counts from pipeline actions, not a body diff.** `new` is `new_findings_total`, the count of findings this tick posted (anchored inline plus relocated unanchored). `fixed` is the number of threads the commit-driven resolution stamped resolved this tick, read as the length of its stamps file (`$SCRATCH/.pr-review-stamps.json`). Both are the tick's first-hand record, so the delta cannot drift from what actually happened and needs no prior-body parse.

2. **`fixed` means fixed-by-this-change, not dismissed.** The count is the commit-driven resolutions (ADR 0017): threads whose flagged defect the fix-check judged gone at HEAD. A thread the operator hand-resolves without a landed fix is not counted, matching the "what your change fixed" framing; it still leaves the cumulative index as resolved, so nothing is lost, only attributed differently.

3. **Shown only on a re-review, and only when it has something to say.** A first review (no prior reviewed SHA) has no "this push versus before": `new` equals the whole finding set and `fixed` is zero, so the line would merely restate the rollup. It is suppressed there. On a re-review the line renders the push's effect; the no-change wording is a rendering decision (Decision 4).

4. **Rendered as index chrome, above the rollup.** The line sits at the top of the findings-index block as italic chrome, keeping the bold `total · open · resolved` rollup the eye's first stop (the italic-chrome / bold-conclusion split of ADR 0002). It is derived and never stored, consistent with ADR 0020's derived-view discipline: recomputed each tick from that tick's actions, holding no state of its own.

## Boundary

This changes what the Status comment *summarizes*, not how findings are detected, judged, or resolved. No new comment and no new marker: it renders inside the existing index block (ADR 0020), reusing the #60 machinery. It adds no PR-level health score (dropped, see Context) and no merge-gate commit status (a separate, parked decision; memory `project_react_doctor_reference` item B). `voice.py` is untouched: the line is fixed UI chrome authored in the renderer, not agent prose, so it sits outside the lexical gate like the other Status-comment chrome (ADR 0010 §3).

## Consequences

- The author sees per-commit progress (`+2 new · 1 fixed`) without re-reading the index or remembering the prior open count.
- One more derived line, no new stored state and no new network call: both counts are already in hand at the render site (`new_findings_total` and the stamps file the resolution step wrote a few lines earlier).
- The one residual noise source is review variance: a finding that flickers out then back across ticks reads as resolved on one tick and new on the next. This is acceptable because the line is informational with no teeth, unlike the parked merge gate, where the same jitter would flicker a merge-blocking status. The line reports what the tick did, which is truthful even when the tick's finding set itself wobbled.
- Extends the ADR 0020 → ADR 0021 arc: the Status comment now carries the cumulative index, the reviewed-SHAs trail, and the per-push delta, three derived views over one edit-in-place comment.
