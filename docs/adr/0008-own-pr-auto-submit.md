# ADR 0008: Own-PR auto-submit; pending retained for others' PRs

Date: 2026-06-02
Status: Proposed

## Context

[ADR 0001](./0001-architecture-baseline.md) D3 posts a single `PENDING` review per PR-tick; the operator submits it. [ADR 0004](./0004-own-pr-review-default.md) leaned on that pending gate to make own-PR review safe ("the pending gate prevents accidental publication of unwanted findings").

In practice the pending-then-web-submit path grew a band-aid cluster: the GitHub web submit modal blanks the review body (#50), so the daemon mirrors the body to a comment as a backup (#49), and posts a transient pickup-ack to cover the gap before the review lands (#48). Each band-aid spawned follow-ups (#58, #59).

Re-examining the gate shows its value is asymmetric. On an **own PR** the gate is ceremony: the operator is reviewing their own code, nothing reaches anyone else, and GitHub blocks self-`APPROVE` so the review can only ever be a `COMMENT`. On **others' PRs** the gate is load-bearing: it stops unvetted findings from landing under the operator's name on a colleague's PR. The daemon already forks on this exact axis via `review_own_prs` (ADR 0004), so keying behavior on own-vs-others is not a new seam.

## Considered options

- **Uniform pending (status quo)**: consistent, but keeps the band-aid cluster on the solo own-PR primary use case, where the gate earns nothing.
- **Auto-submit everywhere**: collapses the band-aids, but removes the gate on others' PRs, where it is load-bearing.
- **Split by own-vs-others (chosen)**: auto-submit own PRs, keep pending for others'.

## Decision

- **Own PR** (author == operator): the daemon submits a `COMMENT` review directly, with no pending stage. The review appearing is itself the "review done" signal; the operator edits or deletes it after the fact rather than vetting it before.
- **Others' PR**: the pending review is retained. The operator submits it via an API path (`gh` / a small helper), not the web modal, so the review body survives.
- Auto-submit on own PRs is the default. A flag to force pending on own PRs is deferred until someone needs it.

This amends ADR 0001 D3 ("always post a single `PENDING` review"): own PRs now post a submitted `COMMENT` review. The bundling rationale of D3 (one Review object carrying body plus inline comments, avoiding per-comment posting) is unchanged.

## Consequences

- The band-aid cluster collapses for the primary solo own-PR case: the pickup-ack (#48) has no gap to bridge and the body mirror (#49) has no wipe to back up. The #49 mirror code is removed; the review-status surface is reworked into an edit-in-place status comment (#60), which is orthogonal to this decision (it covers the review-agent runtime, shared by both paths).
- Supersedes the ADR 0004 consequence "the pending gate prevents accidental publication of unwanted findings" **for own PRs**. Safety there becomes post-hoc edit/delete instead of pre-submit vetting. The `review_own_prs` flag (whether to review own PRs at all) is unchanged.
- The body-wipe (#50) stops affecting either path: own PRs never touch the web modal, others' PRs submit via the API. The #50 operator docs reduce to "submit via the helper, not the web modal."
- The identity model (ADR 0003) is untouched. This is a behavioral refinement on the operator-identity model, not a move to a bot or GitHub App identity.
- On public repos an own-PR self-review is now visible immediately, with no gate. Acceptable for the solo primary use case (own code, own words, editable after the fact); the deferred flag can reintroduce the gate if wanted.
- Submitted `COMMENT` reviews stack per reviewed SHA the way pending reviews did, deduped by SHA (ADR 0006). The operator deletes unwanted ones post-hoc rather than cancelling pending ones.
- The CONTEXT.md **Pending review** definition is split accordingly: pending on others' PRs, a directly submitted `COMMENT` review on own PRs.
