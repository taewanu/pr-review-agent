# ADR 0006 — Sentinel-based dedup for iterative review

Date: 2026-05-26
Status: Draft (outline only — full body lands in phase-5, target 2026-05-29)

## Context

A *sentinel* here means a structured marker — most likely an HTML comment — embedded in the posted review body, carrying metadata (operator login, reviewed SHA, timestamp) that the next tick parses to know what the previous tick did. Same pattern used by Dependabot, Renovate, and GitHub Actions bots to identify their own prior posts.

Phase-4 ships a local state file at `~/.local/state/pr-review-agent/<owner>-<repo>-<pr>.json` that records the last-reviewed HEAD SHA. Same-SHA dedup is simple: if HEAD has not advanced since the last tick, skip the review.

V2 (PRD #21) raises two requirements the state file does not meet:

- **Iterative review needs the *base* SHA, not just the *latest* SHA.** Diff-since-last-review scoping wants to feed the agent only the lines that changed *between* the previous reviewed SHA and HEAD. The state file holds one SHA per PR; the moment a new tick fires, that prior SHA is overwritten, and the base for the next-next tick is lost.
- **State must survive machine moves and multi-operator setups.** A laptop reformat, a daemon migration, or a second operator running their own daemon all break local-only state. The dedup record needs to live where the reviews live — on GitHub.

Open questions to resolve in phase-5:

- What metadata goes in the sentinel? At minimum: operator login, head SHA reviewed, ISO timestamp. Possibly also: review-agent versions, finding count, schema version of the sentinel itself.
- Where in the review body does the sentinel sit — top, bottom, hidden HTML comment, visible footer?
- How does the daemon find prior sentinels — list all reviews on the PR by the operator, parse bodies for the marker, take the most recent?
- What is the migration path from phase-4 state-file dedup? Sentinel-as-source-of-truth with state file as local cache? Or read sentinel once and seed the cache?
- What happens on dropped-combo / pending-conflict failures from ADR 0005 — does the sentinel still get posted, or only on successful reviews?

## Decision

*(deferred to phase-5)*

Likely direction: HTML-comment sentinel embedded in the posted review body, structured as a parseable token so `post-review.sh` writes it and the next tick's `extract-prior-review.sh` (new) reads it. State file becomes a local cache, not the source of truth. Diff-since-last-review uses the prior sentinel SHA as the base.

## Consequences

*(deferred to phase-5)*

Sketch of the dimensions to be filled in:

- Behavior when no prior sentinel exists (first review on a PR — full diff)
- Behavior when sentinel parse fails (treat as no prior sentinel, full diff, log loud)
- Multi-operator interaction (each operator's sentinel keyed by login; daemons read only their own)
- Backwards-compat: phase-4 state-file format and phase-5 sentinel format need to coexist during the transition tick

## Related

- [ADR 0005](./0005-failure-handling-policy.md) — system-vs-per-finding failure taxonomy; sentinel-write failure category to be classified in phase-5
- PRD #21 — V2 acceptance criteria including the 3 JSON-safe reply requirements
- `project_v1_v2_release_plan.md` — phase-5 work breakdown
