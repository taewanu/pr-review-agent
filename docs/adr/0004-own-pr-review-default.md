# ADR 0004 — Own-PR review default behavior

Date: 2026-05-19
Status: Accepted

## Context

An earlier informal decision proposed skipping PRs the operator authored, on the assumption that team members would review each other's PRs. The assumption holds in team contexts but breaks for solo developers, where every PR in the operator's own repos is the operator's own.

Three options were considered:

- **Skip-own (original assumption)** — never review PRs whose author equals the configured operator. Works for teams. Breaks for solo developers, where the daemon would never review anything.
- **Review-own (new default)** — review own PRs alongside others'. The pending review state means findings stay private until the operator submits.
- **Configurable** — let the operator choose per installation.

## Decision

The default behavior is to review own-authored PRs. A configuration flag (`review_own_prs`, default `true`) lets team-context operators opt out so a teammate's daemon handles their PRs instead. The pending review state from [ADR 0001](./0001-architecture-baseline.md) D3 is the safety net that makes this default safe.

## Consequences

- Solo developers get pre-merge AI-drafted self-review without any extra configuration. This is the primary V1 use case.
- Team-context operators set `review_own_prs: false` to fall back to the original "teammates' daemons review my PRs" model.
- For own PRs, the operator effectively uses the daemon as a self-review drafting tool — accepting some findings, discarding others, and submitting (or cancelling) the resulting review.
- GitHub allows the operator to leave a "Comment" type review on their own PR; "Approve" is restricted. The workflow is supported by the platform without special handling.
- The combination of own-PR review and the identity model from [ADR 0003](./0003-identity-model.md) means own-PR reviews appear as self-reviews under the operator's account, marked as AI-drafted via the body marker. The pending gate prevents accidental publication of unwanted findings.
- In team contexts where each member runs their own daemon, multiple daemons may watch the same repo. Each posts independently under its operator's identity; the V1 design has no inter-daemon coordination or merging. A PR may receive several pending reviews — one per active operator — and each operator sees only their own.
