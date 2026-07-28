# ADR 0040: File-level findings, and a merge gate on threads alone

Date: 2026-07-28
Status: Accepted. Amends [ADR 0018](./0018-line-numbered-diff-and-content-anchored-findings.md) (decision 3 gains a fourth outcome, and one consequence was false), [ADR 0005](./0005-failure-handling-policy.md) (relocation rows), [ADR 0020](./0020-findings-index-in-status-comment.md) (decision 4 narrows), [ADR 0033](./0033-per-push-delta-in-status-comment.md) (decision 1's delta buckets), and [ADR 0039](./0039-review-state-on-the-checks-row.md) (decision 3's verdict now reads open threads alone).

## Context

A finding that fails the ADR 0018 anchoring gate has nowhere to live, and the merge gate blocks on it anyway. Both follow from one fact: the pipeline had exactly two destinations, an inline comment or a paragraph in the review body, and the second is not a thread.

**What the paragraph costs.** No `isResolved`, so commit-driven resolution (ADR 0017) cannot close it. No reply box, so the operator's answer lands somewhere else on the page: on [#311](https://github.com/taewanu/pr-review-agent/pull/311) it went to a PR-level comment, detached from the finding it answered. No index entry, because the index is built from threads (ADR 0020 decision 4).

**How often, and which kind.** Over the 80 most recent PRs in `taewanu/pr-review-agent` and `taewanu/sounds-abroad`, 5 carried such a finding, and in 4 of the 5 the file was in the PR's diff. Only the line failed to verify. That is the shape the `intent` role produces by construction: it cites the file a broken description claim is *about*, which the diff usually touches, while the line it names is a claim about code the diff did not change. Severity is no reason to write them off: 2 of the 4 were `important`, one of those a `bug`.

**And the gate is stuck on them.** ADR 0039 put the verdict on the checks row, reading a state derived from `open_findings > 0 || new_findings_total > 0`, where `new_findings_total` counted the body-only findings too. [PR #312](https://github.com/taewanu/pr-review-agent/pull/312) is the proof: every thread resolved, `0 open`, and the `review` check red forever, because the finding holding it red had no thread to resolve. A gate must satisfy "fix it and it opens". That one could not.

## Decision

1. **A finding whose line fails verification but whose file is in the diff becomes a file-level comment.** ADR 0018 decision 3 gains a fourth outcome between "anchor inline" and "relocate to the body": when no line is verifiable and the PR touches the path, post `subject_type: "file"`. This keeps ADR 0018's never-anchor-on-a-guess rule rather than bending it, because a file-level comment claims exactly what a failed quote match justifies: this file, this defect, no line. The emitted line rides along in the finding record untouched, and the comment makes no claim about it.

2. **A file-level comment is its own request, and its failure falls back to the body.** GitHub's REST reference (checked 2026-07-28) lists the `comments[]` properties of `POST /pulls/{n}/reviews` as `path`, `position`, `body`, `line`, `side`, `start_line`, `start_side`, with no `subject_type`; only `POST /pulls/{n}/comments` documents it, where `line` is "Required unless using subject_type:file" and `commit_id` is required. So these cannot ride the review batch. The cost is real and is accepted rather than waved off: one extra request per such finding, and the review stops being the tick's single atomic write.

   The file-level comments post *first*, and any whose request fails are handed to the review body render, which is where they went before this ADR existed. A finding therefore lands on some surface in every outcome, which is the property worth paying for. The residue is that a review POST failing *after* the comments landed leaves them without their review body, and the re-review of that same SHA can duplicate them. That is the rarer failure and the recoverable one; a finding lost to a dropped request is neither.

3. **The status index counts threads and advice apart.** A file-level finding is an ordinary index entry, keyed on its path. A body-only finding keeps ADR 0020 decision 4's pointer treatment, and gets its own bucket in the per-push delta (`+2 new · 1 fixed · +1 advisory`) rather than entering ADR 0033's `new`. Under the old counting it could arrive as `+1 new` and never leave as `1 fixed`, because `fixed` is derived from stamped threads and it has none. A bucket that only fills is a bug in the accounting, not a fact about the PR.

4. **The merge gate reads open threads and nothing else.** The verdict is `block` while any daemon-owned finding thread is open after this tick's resolution, else `pass` (`review_state_for_open_threads` in `lib.sh`). An open thread is the one signal that satisfies "fix it and the gate opens", and with decision 1 shipped nearly every finding has one.

   Severity is not an input either, and that is a separate call from the one above. This session found two `nit`-graded findings whose real impact was large, so the grading is not trustworthy enough to carry merge authority. Severity orders reading; it does not decide merging.

5. **A finding on a file the PR never touched is advisory by design, not a gap.** GitHub accepts no comment there, so the constraint is not ours to route around, but the judgement stands on its own: such a finding reads as "I noticed something over there", not "this change broke that". Gating a merge on it would be wrong even if the API allowed it. The next reader should file this as a decision, not a defect.

## Boundary

This ADR decides where a finding lands and what the daemon's own verdict reads. Whether that verdict gates a merge is still branch protection, an operator choice (ADR 0039's boundary, unchanged). It does not touch the fix-check judge, which already takes `{path, line, finding_body}` and treats the line as "a starting hint, not a fixed address"; a file-level candidate simply passes `null` there.

## Consequences

- **ADR 0018's stated reason for accepting the loss was false and is corrected.** It said an unverifiable anchor "could never auto-resolve correctly anyway". The judge never needed a verified line; what was missing was a candidate record, since `select_candidates` reads the thread array and dropped every thread without an integer line. That guard is relaxed, and a file-level thread routes through the untouched bucket.

- **File-level candidates compete for the untouched judgment budget.** They can never be `touched` (no line to test against the increment's hunks), so they queue behind `RESOLVE_UNTOUCHED_CAP` with the rest. At the measured rate, about one such finding per affected review, the cap is not the binding constraint; if that changes, the cap is the dial.

- **The split logs why each file-level finding lost its line, and nothing acts on it yet.** A finding that named a `quote` cited a line the diff does not contain, which is a generation defect this surface relocates rather than repairs; one that named none is region-level by construction. The two are indistinguishable in a posted review because the quote is dropped at render time, so `anchor_findings.py` counts them at the split and `review-pr.sh` logs the split. The ratio decides whether the next move here is a surface or a prompt fix, and that question stays open (#191).

- **A dry-run shows file-level findings in the body it prints.** Nothing posts, so the preview folds them into the one payload it has rather than dropping a finding from the only surface a dry-run has. The eval harness is unaffected: it reads the pre-split payload (`emit_dryrun_contract`).

- **A review whose findings are all file-level posts a body with no findings section.** The summary and the footer still land, and the findings live in their own threads. One source per fact, as with inline comments.

- **A thread fetch that fails leaves the verdict standing on this tick's own count.** With no thread list to read, the verdict falls back to the number of threads this tick created, which sees nothing of the threads earlier ticks left open. A degraded tick can therefore report `pass` over an open backlog. The fetch failure is logged, and the next reviewed SHA recomputes from a fresh fetch; the same blind spot existed before this ADR, which used the raw new-findings count for the same fallback.
