# ADR 0039: Review state on the PR's checks row

Date: 2026-07-27
Status: Accepted. Amends [ADR 0005](./0005-failure-handling-policy.md) (failure table) and [ADR 0036](./0036-github-app-identity.md) (permission set). Decision 3's verdict is narrowed by [ADR 0040](./0040-file-level-findings-and-a-gate-on-threads.md): it reads open finding threads alone, so a review whose only findings are advisory now concludes `success`. The two-surface split and the conclusion mapping stand. Amended 2026-11-13: when the open finding threads change after the review, both surfaces follow them (decision 6), so a reply pass that resolves a thread re-derives the verdict; the consequence that `reply-pr.sh` had nothing to put on this surface is withdrawn.

## Context

The review's result lived only in comments. The checks row, the line a reader scans before merging, said nothing about it, so "has the agent looked at this yet, and did it pass" cost a scroll into the conversation every time.

[#48](https://github.com/taewanu/pr-review-agent/issues/48) wanted the checks row already and could not have it: check runs are App-only, and the daemon then posted under the operator's account, so the pickup announcement became a comment instead. ADR 0036 removed that constraint and left the door explicitly unopened: "The checks and statuses surface opens but is not taken. The permissions come when the feature does."

The announcement has since moved once more. The transient comment #48 posted and deleted is gone: [#60](https://github.com/taewanu/pr-review-agent/issues/60) folded it into the durable status comment's `👀 Reviewing …` head-line, which the same comment then edits into the verdict. So there is no comment to retire here, only a state that is being announced on a surface that cannot show state without being read.

## Decision

The review runs on two surfaces with different jobs.

1. **The checks row carries state, and only state.** One check run named `review`, opened `in_progress` before the multi-minute review and concluded once by the review (*amended 2026-11-13:* then updated only as decision 6 allows), with a title naming the state it is in. Everything a reader has to read (the findings index, the scope, the reviewed-SHAs trail, the per-push delta: [ADR 0020](./0020-findings-index-in-status-comment.md), [ADR 0021](./0021-reviewed-shas-trail-in-status-comment.md), [ADR 0033](./0033-per-push-delta-in-status-comment.md)) stays in the status comment, and the run's `details_url` links there. A row that restated the comment would be noise on a surface with room for a handful of entries, and a second copy of facts that would then be free to disagree.

2. **The `in_progress` run is the pickup announcement, in the shape the surface already has.** Nothing to post and nothing to delete, which is what made the comment form of it awkward. The status comment's `👀 Reviewing …` head-line stays: it anchors the scope and the trail the comment carries, and a reader who opens the comment still needs to see which commit is under review.

3. **The conclusion mirrors the verdict the review already computes.** `review-pr.sh` derives one `block`-or-`pass` state from the open findings and renders the status head-line from it; the check run reads the same state, so the two surfaces cannot disagree. `pass` maps to `success`, `block` to `failure`.

   `failure` rather than `neutral` because GitHub treats neutral as a pass: a neutral block would settle the gating question by making the check useless as a required check, and that question belongs to whoever configures branch protection. `failure` reports the verdict and leaves the gate to them. Not `action_required` either, whose call-to-action framing overstates a review whose findings may all be nits.

4. **No run of `review-pr.sh` may leave a check run `in_progress`.** A stale status comment misinforms; a stuck check run can hold a merge back indefinitely, on a PR whose review is long dead. The EXIT trap therefore concludes any still-open run as `neutral` on every exit path, not only a failing one, and runs that handler before the status-comment flip, because these handlers may be racing the per-PR watchdog's escalation from TERM to KILL. `neutral` is the honest conclusion: no verdict was reached, and a daemon-side crash is not a claim about the PR ([ADR 0005](./0005-failure-handling-policy.md)).

   `review-pr.sh` also traps `TERM` and `INT` now. A signal kills the shell without running the EXIT trap unless it is trapped, and the signalled death is the case that matters: the per-PR watchdog sends TERM before it escalates, and Ctrl-C on the foreground daemon sends INT.

5. **`checks: write` joins ADR 0036 decision 3's permission set.** It is what the check-run endpoints require and it grants nothing else.

6. **When the open finding threads change after the review, both surfaces follow.** *Added 2026-11-13.* A thread resolved by a reply left the check at `failure`, "Findings open on this commit", and the status comment at "you shall not pass" with `1 open`, until a later commit was reviewed; a repository that requires the check had to push a commit only to be re-reviewed. The verdict reads open finding threads alone ([ADR 0040](./0040-file-level-findings-and-a-gate-on-threads.md) decision 4), and the reply pass changes exactly that input.

   After posting, `reply-pr.sh` calls `refresh_review_verdict` (`lib.sh`). It reads the threads fresh and derives the state once, with the same `count_open_findings` and `review_state_for_open_threads` the review uses. It then brings each surface to that state where the surface differs: first the status comment's head-line and rollup, then the `review` run. Each surface is compared on its own, so a run an earlier write left stale is repaired even when the comment is already current. The run is not written after a comment edit that did not land, so the check never shows a verdict the comment does not. The scope, the per-push delta, the outside-the-diff pointer, and the trail describe the review rather than the threads, so they stay as posted.

   A review whose own thread read failed renders no rollup. The refresh adds one under the scope line, so a head-line it changes always rests on an index. A comment in any other form it cannot bring to the threads is logged as such and left, and the check with it.

   - **Only the reviewed commit, and only while it is the PR's HEAD.** The sentinel in the status comment ([ADR 0024](./0024-status-comment-sentinel-for-zero-finding-dedup.md)) names the reviewed commit, and a Reviewing or failed render carries none, so neither is touched. The reply pass reads HEAD before its agent runs, so the refresh reads it again just before its writes and skips when HEAD has moved or the read fails. When HEAD has moved, the next review concludes its own run; the older commit's run keeps its conclusion, which gates nothing, because GitHub evaluates required checks on the latest commit ("Troubleshooting required status checks", GitHub Docs, read 2026-11-13). A push can still land between that re-read and the writes, leaving the comment with the older commit's verdict; the review of the new commit repairs it, since it concludes its own run and re-renders the comment.
   - **The newest run is updated in place, not superseded.** The refresh lists the runs on the commit by check name and App id, and patches the one with the highest id: the run the latest review of the commit opened. An earlier attempt's run keeps the conclusion it records. A second run of the same name would be one call cheaper, but GitHub's REST reference for check runs (read 2026-11-13) does not say which of several same-name runs a required check reads. The lookup passes `filter=all` because the same reference describes the default, `latest`, only as "the most recent check runs".
   - **A run still `in_progress` is left; a `neutral` one is brought to the verdict.** The review that opened a run is the one that ends it (decision 4). A `neutral` run is different: the refresh reaches the run only when the status comment carries the sentinel, which only a review that finished stamps, so a `neutral` run there is one the review left when it stopped between its comment edit and its conclude. That review had a verdict; the run just never received it. A review that stopped before its verdict leaves a Reviewing or failed render with no sentinel, and the refresh touches neither surface.
   - **Only on a reply pass that reached posting.** A thread resolved or reopened in the GitHub UI with no reply shows on both surfaces at the next reply pass or review. Covering it every polling cycle would add four reads per open PR per cycle, each a call that can hang.
   - **A review in flight owns both surfaces.** The refresh takes the review's per-PR lock without waiting and skips while a review holds it, because that review reads the threads itself before it writes.
   - **Best-effort, like the writes in decision 4.** A failed refresh logs and returns; the reply pass and its posted replies stand. Each write is logged as landed or not, and a failed run lookup is logged as a failed lookup, not as a missing run.

## Boundary

Whether `main` requires the check is branch protection, an operator choice, and no code here assumes either answer. The daemon reports a verdict; the repo decides whether the verdict gates.

## Consequences

- **A nit-only review shows a red X.** That is the claim the status head-line already makes for the same state ("you shall not pass" whenever any finding thread is open), so the row is consistent with the comment rather than harsher than it. Deriving a softer conclusion from severity would be a second verdict, which decision 3 exists to prevent. ADR 0040 decision 4 re-examined the severity question directly and kept this answer, on the evidence that the grading is not reliable enough to carry merge authority.

- **A `SIGKILL`ed review leaves the row `in_progress` until the same SHA is reviewed again.** The trap covers every exit the process controls, including the watchdog's TERM, but nothing survives KILL. A failed tick stamps no sentinel ([ADR 0006](./0006-sentinel-based-dedup.md)), so the next cycle re-reviews that SHA and opens a fresh run; until then, a repo that requires the check sees a pending gate. An operator who stops the daemon with a run open clears it the same way: by letting a later review of that commit land.

- **The checks row degrades on its own.** Creating and concluding the run are best-effort like the status comment, so an installation without `checks: write` still gets its reviews, with one log line naming what was skipped.

- **The dry-run paths open no run**, matching how they skip the status comment: `--dry-run` and `--at-sha` post nothing.

- ~~**`reply-pr.sh` is untouched.** The operator-reply ack pass computes no verdict, so it has nothing to put on this surface.~~ *Withdrawn 2026-11-13:* the reply pass computes no verdict of its own, but it changes the verdict's only input when it resolves a thread, so it re-derives the verdict (decision 6).

- **A manual run that overlaps a review can leave the older verdict.** Within the daemon the two never overlap: `poll.sh` runs a PR's reply pass before dispatching its review and waits for every review before the cycle ends. A manual one-shot can overlap one; if the review read the threads before the reply resolved one, the refresh skips on the review's lock and the review writes the verdict it read. The next reply pass or review corrects it.

- **A reply pass that posts makes four more reads**: the PR's issue comments, its threads, its head commit, and that commit's `review` runs. A surface that differs from the verdict adds its write. A polling cycle with no reply makes no extra call.

- **Fork PRs are untested here.** The run is created on the base repo, where the installation and the PR's checks row live, and the head commit of a fork PR is reachable there through `refs/pull/<n>/head`; whether the API accepts it in that shape has not been exercised. A rejection degrades to no row, not a failed review.
