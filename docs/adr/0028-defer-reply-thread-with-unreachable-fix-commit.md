# ADR 0028: Defer a reply thread whose claimed fix commit is not in HEAD yet

Date: 2026-07-08
Status: Accepted.

## Context

The reply agent pushed back on a correct fix because it verified against pre-fix code. On `taewanu/sounds-abroad#163` the operator replied `Fixed in 115dbb3.` to a finding; the reply agent answered that "neither half of the change landed," citing `527d59e` (the parent of `115dbb3`). Both halves had in fact landed. The operator was right and the agent was wrong.

The cause is a race between the operator's push and the reply pass's fetch. `reply-pr.sh` resolves `HEAD_OID` fresh each run via `gh pr view --json headRefOid`, then clones at that SHA for the agent to read. The commit timeline shows `115dbb3` committed at 03:38:37 and the operator's reply naming it at 03:39:54, but the reply pass read `527d59e` content at 03:41:46, meaning the remote PR head was still `527d59e` when the pass fetched it: the operator committed and replied before the push reached the branch. The agent read the only code it could see, the pre-fix parent, and `review-agent-reply`'s contract (a claim that "still shows the old shape" is a `pushback`) then did exactly the wrong thing.

The failure is worse than a review-time false positive. A false positive costs a comment the operator dismisses; a false pushback tells a correct operator their fix did not land, which erodes trust in the agent's verification. The `review-agent-fix-check` path already guards its analogue with a safe bias (leave the thread open under any doubt); the reply path had no symmetric guard, so it asserted "not fixed" against code that predated the claimed fix.

## Considered options

- **Instruct the reply agent to notice the mismatch (rejected).** Tell the prompt to compare the claimed SHA against its checkout and defer when they differ. This is the judgment-level prompt lever ADR 0016 found has a ceiling (`project_voice_iteration_limits`): a "check the SHA" instruction competes with the rest of the reply contract and cannot be relied on, where the daemon can enforce the same check deterministically before the agent ever runs.
- **Do nothing; the window is small (rejected).** The race needs the operator to reply before the push lands, so it is rare. But when it fires, the wrong pushback is already posted and stays, and the cost (a correct operator told they are wrong) is high enough that a cheap deterministic guard is worth it.
- **Defer the thread when its claimed fix commit is not in HEAD (chosen).** Move the check out of the agent and into `reply-pr.sh`, gated on ancestry the daemon can verify.

## Decision

Skip a reply thread this cycle when the operator's reply names a fix commit that is not yet reachable from the reviewed HEAD, and redispatch it next cycle.

1. **`reply_defers_on_unreachable_fix` (`daemon/lib.sh`)** takes the operator reply body, the head repo, and `HEAD_OID`, and returns defer / verify-now. It extracts the commit the reply claims as its fix, then defers only when that commit is not an ancestor of HEAD.

2. **Reachability reuses `is_fast_forward` (the compare API), not the local clone.** The per-PR clone is `--depth=1`, so `git merge-base --is-ancestor` cannot prove ancestry and would read every case as unreachable (#149, the same shallow-history trap that moved the fast-forward decision to the server). Asking `repos/{repo}/compare/{sha}...{HEAD}` answers "is this commit in HEAD's history" from full history; an unpushed SHA the API cannot resolve falls through to not-an-ancestor, which is exactly the race case, so the same helper covers both "not pushed yet" and "pushed but diverged."

3. **`reply-pr.sh` filters threads before cloning.** Each thread runs through the predicate; a deferred thread is dropped from this cycle's batch and logged. If every thread defers, the pass exits `0` before doing any work, the same shape as the no-unaddressed-replies exit. A deferred thread is never acked or resolved, so poll.sh's existing redispatch picks it up next cycle once the push lands and the fix is reachable. The defer is self-healing, not a terminal skip.

4. **Extraction is anchored to the claim's shape, biased to miss rather than over-match.** A claimed commit is a 7-40 hex run that is either backtick-delimited (`` `115dbb3` ``, or the `` `sha:Lnn` `` link form the agent's own replies use) or inside a github `commit`/`blob` URL. This asymmetry is deliberate: a spurious match (prose "the deadbeef case", a color `` `#fff` ``, a backticked identifier `` `step(dir)` ``) would make `is_fast_forward` fail forever and defer a legitimate thread on every cycle, a permanent hang, while a missed match (an unbackticked SHA, a fix described without a commit reference) just falls through to the pre-existing verify-now behavior. Under-matching is safe; over-matching is not.

## Boundary

This guards only the named-commit case. A fix claim that names no commit, or names one unbackticked, is not deferrable by this predicate and verifies immediately as before, so the race is narrowed, not closed, for those replies. It does not change the reply agent's contract (ADR 0019), the fix-claim / question / acknowledgment classification, the voice gate (ADR 0010), or resolution and stamping. It touches only which threads reach the agent, not what the agent does with them.

## Consequences

- A correct fix claim is never contradicted by pre-fix code: the thread waits until the fix is reachable, then verifies against it.
- One extra `is_fast_forward` call per thread that names a commit, on the compare API, before the clone. Threads that name no commit skip it. Cost is one network round trip on the reply path's common case (a fix claim), amortized against skipping the clone entirely when every thread defers.
- The extraction regex is the change's whole risk surface, and its bias is toward the safe direction. The tests (`tests/test_reply_defer.py`) pin both the defer decision and the adversarial hex that must not match; live dogfood covers the compare-API half, as it does for `is_fast_forward` itself (#123, #149).
- The residual gap (unbackticked or commitless claims still race) is acceptable: a fix claim that links or backticks its commit carries a matchable reference, which is the reply agent's own posted convention, so the common case is covered and the unmatched tail keeps the old behavior rather than a worse one.
