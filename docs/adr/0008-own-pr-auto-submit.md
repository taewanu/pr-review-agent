# ADR 0008: Own-PR auto-submit; pending retained for others' PRs

Date: 2026-06-02
Status: Accepted

## Context

[ADR 0001](./0001-architecture-baseline.md) D3 posts a single `PENDING` review per PR-tick; the operator submits it. [ADR 0004](./0004-own-pr-review-default.md) leaned on that pending gate to make own-PR review safe ("the pending gate prevents accidental publication of unwanted findings").

In practice the pending-then-web-submit path grew a band-aid cluster: the GitHub web submit modal blanks the review body (#50), so the daemon mirrors the body to a comment as a backup (#49), and posts a transient pickup-ack to cover the gap before the review lands (#48). Each band-aid spawned follow-ups (#58, #59).

Re-examining the gate shows its value is asymmetric. On an **own PR** the gate is ceremony: the operator is reviewing their own code, nothing reaches anyone else, and GitHub blocks self-`APPROVE` so the review can only ever be a `COMMENT`. On **others' PRs** the gate is load-bearing: it stops unvetted findings from landing under the operator's name on a colleague's PR. The daemon already forks on this exact axis via `review_own_prs` (ADR 0004), so keying behavior on own-vs-others is not a new seam.

## Considered options

- **Uniform pending (status quo)**: consistent, but keeps the band-aid cluster on the solo own-PR primary use case, where the gate earns nothing.
- **Auto-submit everywhere**: collapses the band-aids, but removes the gate on others' PRs, where it is load-bearing.
- **Split by own-vs-others (chosen)**: auto-submit own PRs, keep pending for others'.

## Decision

- **Own PR** (author == operator): the daemon submits a `COMMENT` review directly, with no pending stage. The review appearing is itself the "review done" signal; the operator edits it after the fact rather than vetting it before. Note that a submitted review cannot be deleted (see Consequences), so an unwanted one is edited or has its comments hidden, not removed.
- **Others' PR**: the pending review is retained. The operator submits it via an API path (`gh` / a small helper), not the web modal, so the review body survives.
- Auto-submit on own PRs is the default. A flag to force pending on own PRs is deferred until someone needs it.

This amends ADR 0001 D3 ("always post a single `PENDING` review"): own PRs now post a submitted `COMMENT` review. The bundling rationale of D3 (one Review object carrying body plus inline comments, avoiding per-comment posting) is unchanged.

## Consequences

- The band-aid cluster collapses for the primary solo own-PR case: the pickup-ack (#48) has no gap to bridge and the body mirror (#49) has no wipe to back up. The #49 mirror code is removed; the review-status surface is reworked into an edit-in-place status comment (#60), which is orthogonal to this decision (it covers the review-agent runtime, shared by both paths).
- Supersedes the ADR 0004 consequence "the pending gate prevents accidental publication of unwanted findings" **for own PRs**. Safety there becomes post-hoc editing instead of pre-submit vetting. The `review_own_prs` flag (whether to review own PRs at all) is unchanged.
- **A submitted review cannot be deleted.** Both the REST `DELETE .../reviews/:id` and the GraphQL `deletePullRequestReview` mutation reject a non-pending review with `422 "Can not delete a non-pending pull request review"`. So an unwanted auto-submitted review on an own PR is recoverable only by editing its body or hiding individual comments, never by removal. Auto-submit on own PRs is therefore effectively irreversible. Acceptable for the solo primary use case (own code, own words, editable after the fact); the deferred force-pending flag is the escape hatch if this bites.
- Auto-submit forgoes the implicit "one pending review per PR" lock. That constraint used to serialize concurrent reviews of the same PR: a second review POST hit a pending-conflict and bailed. A submitted `COMMENT` review has no such limit, so two reviewers racing on the same PR both post. In normal operation this cannot happen (launchd `StartInterval` serializes daemon ticks, and same-SHA dedup per ADR 0006 skips re-reviews across ticks); it surfaces only under operator-induced concurrency, such as a manual `review-pr.sh` run overlapping a daemon tick. Closing that window (a per-PR lock plus a pre-submit dedup check in `review-pr.sh`) is tracked separately.
- The body-wipe (#50) stops affecting either path: own PRs never touch the web modal, others' PRs submit via the API. The #50 operator docs reduce to "submit via the helper, not the web modal."
- The identity model (ADR 0003) is untouched. This is a behavioral refinement on the operator-identity model, not a move to a bot or GitHub App identity.
- On public repos an own-PR self-review is now visible immediately, with no gate. Acceptable for the solo primary use case (own code, own words, editable after the fact); the deferred flag can reintroduce the gate if wanted.
- Submitted `COMMENT` reviews stack per reviewed SHA the way pending reviews did, deduped by SHA (ADR 0006). The operator edits or hides unwanted ones post-hoc rather than cancelling pending ones (they cannot be deleted, per the consequence above).
- The CONTEXT.md **Pending review** definition is split accordingly: pending on others' PRs, a directly submitted `COMMENT` review on own PRs.
