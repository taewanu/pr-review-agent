# ADR 0017: Commit-driven thread resolution

Date: 2026-06-13
Status: Accepted

## Context

Thread resolution today is reply-driven (#75): the daemon resolves a Reply thread only when an Operator reply produces a `confirmed` or `withdrawn` verdict. A Finding fixed in a later commit *without any reply* leaves its conversation open. `main` is branch-protected on "Require conversation resolution before merging," so a fixed-but-unreplied Finding blocks the merge until someone resolves it by hand. This is the commit-driven complement deferred from #75.

The core risk shapes the whole design: auto-resolving a still-live Finding. A later commit can touch the flagged code without fixing the defect, and resolving on that signal both hides a real bug and clears a merge gate. So a wrongly-closed live Finding is the dangerous failure; a wrongly-left-open fixed Finding is the harmless one, recoverable by a hand click. Every choice below biases errors toward the harmless side.

## Considered options

- **Mechanical signal — the anchor line changed, so GitHub flags the comment "outdated" (rejected).** Cheap, no agent call, but touched is not fixed. It resolves live Findings on the exact failure the feature must avoid, with nothing to catch it.
- **Re-judge every open daemon thread on each new SHA (rejected).** Accurate, but it pays an agent call to re-examine Findings nothing has touched. The incremental-diff filter below buys the same accuracy on a far smaller candidate set.
- **Reuse the Editor agent to also adjudicate prior open threads (rejected).** The Editor's contract is cut, reword, and reconcile over the *current* review's Findings (ADR 0016). Adjudicating a prior thread is a different input (the Finding's text plus the file at HEAD, not the incremental diff) and a different responsibility; folding both into one agent muddies the contract and loses per-thread isolation and parallelism.
- **Silent resolve, no trace (rejected).** A wrongly-closed live Finding then vanishes with no way for the Operator to notice or reverse it. This discards the audit safety net that makes the auto-close acceptable.
- **Command-driven only, like CodeRabbit's `@coderabbitai resolve` (rejected).** Safe, but it defeats the purpose: the Operator still acts by hand, only with a command instead of a click. The point of the feature is hands-off resolution.

## Decision

Add a commit-driven thread-resolution stage on the review path (it runs on a new HEAD SHA, where a commit-borne fix appears), built from three safety layers over a best-effort failure model.

1. **Candidate by incremental diff.** A candidate is an open, daemon-owned review thread whose anchor line falls inside the current increment's diff (`last_reviewed_sha..HEAD`; the full PR diff on force-push or rebase). Tying candidacy to *this* increment, rather than to GitHub's sticky "outdated" flag, makes the stage idempotent: a fixing commit judges each thread it touched exactly once, and an unfixed thread is re-judged only when a later commit touches it again. It needs no new persistence — the review path already computes this diff.

2. **Per-candidate judgment, safe-biased (safety layer 1).** For each candidate, a dedicated agent re-reads the Finding's location at HEAD on a fresh context and judges whether *that specific defect* is gone, returning a one-line rationale. It is separate from the Editor to keep that contract clean and to isolate and parallelize per-thread judgments. The judgment defaults to leave-open under any uncertainty, so errors fall toward the harmless side.

3. **Note, then resolve (safety layer 2).** On a fix verdict the daemon posts a threaded `_Fixed:_` note carrying the one-line rationale and the HEAD SHA, then resolves the thread. The note rides the existing reply-posting path (`create_reply.py`), so it is voice-gated (`voice.py`), carries the Provenance tag, and follows the #106 reply-lead format (italic colon-lead, no trailing period). `_Fixed:_` is a new lead, distinct from the four reply Verdicts — those are all responses to an Operator reply, and this one has none — and distinct from "resolved," which names the GitHub state. The note is the audit trace: a wrongly-closed Finding stays visible and reversible, never silent.

4. **Best-effort resolve with idempotent retry.** The note lands reliably; the resolve mutation transiently drops under rate-limit (`project_reply_batch_burst_transient`). So the order is note-then-resolve, and a thread that already carries a `_Fixed:_` note but is still open is re-resolved on the next tick — resolve only, with no re-judgment and no second note, since the note's Reply sentinel blocks a duplicate. This closes the stuck-open state (a `_Fixed:_` note over an open thread) that would otherwise defeat the merge-unblock purpose. Notes are wrapped in the per-tick batched COMMENT review (#38) so a tick costs one notification.

5. **No guards.** No severity gate and no own-PR-versus-others' exclusion. A severity gate would leave high-severity Findings blocking the merge — the exact problem the feature removes — and the per-thread judgment already gates on "actually fixed." The thread resolved is always the daemon's own Finding's thread, so the others'-PR case is symmetric with the reply-driven path (#75). Safety rests on the judgment, the note, and the safe bias, not on scope carve-outs.

## Boundary

This ADR decides commit-driven resolution only. It does not change the reply-driven path (#75), the Verdict vocabulary, the per-cycle disposition summary (#11, which stays reply-driven), or `voice.py`'s rules. It adds no cumulative per-PR counter (out of scope per #125). It accepts the flagged-line false-negative: a fix that lands away from the Finding's own line never makes its thread a candidate, so that thread stays open for the Operator to close by hand.

## Consequences

- A Finding fixed by a commit no longer blocks the merge or clutters the PR; the Operator gets the same hands-off resolution as the reply-driven path, with an audit note in place of a Verdict ack.
- Each new SHA spends one judgment agent call per thread whose code changed in that increment, and zero on a no-op SHA. The incremental-diff filter bounds the cost.
- The stage runs under the Operator user token (ADR 0003), which can call `resolveReviewThread`. A future GitHub App pivot (v0.3.0) would gain the Checks API and could reshape the resolution surface; this design stays token-only and does not depend on it.
- "Thread resolution" now carries two drivers — reply-driven (a Verdict) and commit-driven (a fix detected at HEAD, no reply) — recorded in CONTEXT.md.
