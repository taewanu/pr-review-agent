# ADR 0039: Review state on the PR's checks row

Date: 2026-07-27
Status: Accepted. Amends [ADR 0005](./0005-failure-handling-policy.md) (failure table) and [ADR 0036](./0036-github-app-identity.md) (permission set). Decision 3's verdict is narrowed by [ADR 0040](./0040-file-level-findings-and-a-gate-on-threads.md): it reads open finding threads alone, so a review whose only findings are advisory now concludes `success`. The two-surface split and the conclusion mapping stand.

## Context

The review's result lived only in comments. The checks row, the line a reader scans before merging, said nothing about it, so "has the agent looked at this yet, and did it pass" cost a scroll into the conversation every time.

[#48](https://github.com/taewanu/pr-review-agent/issues/48) wanted the checks row already and could not have it: check runs are App-only, and the daemon then posted under the operator's account, so the pickup announcement became a comment instead. ADR 0036 removed that constraint and left the door explicitly unopened: "The checks and statuses surface opens but is not taken. The permissions come when the feature does."

The announcement has since moved once more. The transient comment #48 posted and deleted is gone: [#60](https://github.com/taewanu/pr-review-agent/issues/60) folded it into the durable status comment's `👀 Reviewing …` head-line, which the same comment then edits into the verdict. So there is no comment to retire here, only a state that is being announced on a surface that cannot show state without being read.

## Decision

The review runs on two surfaces with different jobs.

1. **The checks row carries state, and only state.** One check run named `review`, opened `in_progress` before the multi-minute review and concluded once, with a title naming the state it is in. Everything a reader has to read (the findings index, the scope, the reviewed-SHAs trail, the per-push delta: [ADR 0020](./0020-findings-index-in-status-comment.md), [ADR 0021](./0021-reviewed-shas-trail-in-status-comment.md), [ADR 0033](./0033-per-push-delta-in-status-comment.md)) stays in the status comment, and the run's `details_url` links there. A row that restated the comment would be noise on a surface with room for a handful of entries, and a second copy of facts that would then be free to disagree.

2. **The `in_progress` run is the pickup announcement, in the shape the surface already has.** Nothing to post and nothing to delete, which is what made the comment form of it awkward. The status comment's `👀 Reviewing …` head-line stays: it anchors the scope and the trail the comment carries, and a reader who opens the comment still needs to see which commit is under review.

3. **The conclusion mirrors the verdict the review already computes.** `review-pr.sh` derives one `block`-or-`pass` state from the open findings and renders the status head-line from it; the check run reads the same state, so the two surfaces cannot disagree. `pass` maps to `success`, `block` to `failure`.

   `failure` rather than `neutral` because GitHub treats neutral as a pass: a neutral block would settle the gating question by making the check useless as a required check, and that question belongs to whoever configures branch protection. `failure` reports the verdict and leaves the gate to them. Not `action_required` either, whose call-to-action framing overstates a review whose findings may all be nits.

4. **No run of `review-pr.sh` may leave a check run `in_progress`.** A stale status comment misinforms; a stuck check run can hold a merge back indefinitely, on a PR whose review is long dead. The EXIT trap therefore concludes any still-open run as `neutral` on every exit path, not only a failing one, and runs that handler before the status-comment flip, because these handlers may be racing the per-PR watchdog's escalation from TERM to KILL. `neutral` is the honest conclusion: no verdict was reached, and a daemon-side crash is not a claim about the PR ([ADR 0005](./0005-failure-handling-policy.md)).

   `review-pr.sh` also traps `TERM` and `INT` now. A signal kills the shell without running the EXIT trap unless it is trapped, and the signalled death is the case that matters: the per-PR watchdog sends TERM before it escalates, and Ctrl-C on the foreground daemon sends INT.

5. **`checks: write` joins ADR 0036 decision 3's permission set.** It is what the check-run endpoints require and it grants nothing else.

## Boundary

Whether `main` requires the check is branch protection, an operator choice, and no code here assumes either answer. The daemon reports a verdict; the repo decides whether the verdict gates.

## Consequences

- **A nit-only review shows a red X.** That is the claim the status head-line already makes for the same state ("you shall not pass" whenever any finding thread is open), so the row is consistent with the comment rather than harsher than it. Deriving a softer conclusion from severity would be a second verdict, which decision 3 exists to prevent. ADR 0040 decision 4 re-examined the severity question directly and kept this answer, on the evidence that the grading is not reliable enough to carry merge authority.

- **A `SIGKILL`ed review leaves the row `in_progress` until the same SHA is reviewed again.** The trap covers every exit the process controls, including the watchdog's TERM, but nothing survives KILL. A failed tick stamps no sentinel ([ADR 0006](./0006-sentinel-based-dedup.md)), so the next cycle re-reviews that SHA and opens a fresh run; until then, a repo that requires the check sees a pending gate. An operator who stops the daemon with a run open clears it the same way: by letting a later review of that commit land.

- **The checks row degrades on its own.** Creating and concluding the run are best-effort like the status comment, so an installation without `checks: write` still gets its reviews, with one log line naming what was skipped.

- **The dry-run paths open no run**, matching how they skip the status comment: `--dry-run` and `--at-sha` post nothing.

- **`reply-pr.sh` is untouched.** The operator-reply ack pass computes no verdict, so it has nothing to put on this surface.

- **Fork PRs are untested here.** The run is created on the base repo, where the installation and the PR's checks row live, and the head commit of a fork PR is reachable there through `refs/pull/<n>/head`; whether the API accepts it in that shape has not been exercised. A rejection degrades to no row, not a failed review.
