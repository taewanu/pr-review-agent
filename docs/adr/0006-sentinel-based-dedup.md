# ADR 0006 — Sentinel-based dedup for iterative review

Date: 2026-05-28
Status: Accepted

## Context

A *sentinel* here means a structured marker — an HTML comment — embedded in the posted review body, carrying the reviewed SHA so the next tick can parse it and know what the previous tick did. Same pattern used by Dependabot, Renovate, and GitHub Actions bots to identify their own prior posts.

Phase-4 ships a local state file at `~/.local/state/pr-review-agent/<owner>-<repo>-<pr>.json` that records the last-reviewed HEAD SHA. Same-SHA dedup is simple: if HEAD has not advanced since the last tick, skip the review.

V2 (PRD #21) raises two requirements the state file does not meet:

- **Iterative review needs the *base* SHA, not just the *latest* SHA.** Diff-since-last-review scoping wants to feed the review agent only the lines that changed between the previous reviewed SHA and HEAD. The state file holds one SHA per PR; when a new tick fires, that prior SHA is overwritten and the base for the next-next tick is lost.
- **State must survive machine moves and multi-operator setups.** A laptop reformat, a daemon migration, or a second operator running their own daemon all break local-only state. The dedup record needs to live where the reviews live — on GitHub.

## Decision

Embed a sentinel HTML comment in every posted review body, encoding the reviewed HEAD SHA. The sentinel is the source of truth for "what SHA was last reviewed". The phase-4 state file becomes a fallback cache through phase-5 and is removed in phase-6 or later.

### Sentinel format

Single-key HTML comments under a `pr-review-agent` namespace.

    <!-- pr-review-agent:sha:<40-char-hex-sha> -->

- One marker, one fact. Multiple markers per body OK; each is grepped by an independent regex.
- ASCII alphanumeric + `:` + `-` only — no quotes, braces, or non-ASCII inside the comment, so GitHub's markdown sanitizer round-trips it unchanged.
- Operator identity comes from the review's `user.login` field on the API response; it does not need to be repeated inside the sentinel.
- Timestamp comes from the review's `submitted_at` / `created_at` field; same reason.

The same namespace covers `<!-- pr-review-agent:addressed -->`, the reply-thread acknowledgement marker introduced by the later reply-subagent ADR (PRD #21 acceptance criterion). This ADR fixes the format so the later ADR does not have to revisit it.

### Placement

Review body footer, immediately after the operator identity line from [ADR 0003](./0003-identity-model.md):

    [banner / summary / additional findings]

    ---

    🤖 Drafted by [<project>](<project-url>). Submit, edit, or cancel as needed.
    <!-- pr-review-agent:sha:abc1234567890abcdef1234567890abcdef12345678 -->

The `<project>` and `<project-url>` slots are derived per fork from `git remote get-url origin`. The `pr-review-agent:` sentinel namespace is fixed across forks so the format stays parseable everywhere.

The review body is the only surface GitHub guarantees on every review, including pending reviews with zero inline comments. First-inline placement breaks on findings-0 reviews and the server does not guarantee inline ordering.

### Discovery

Per-tick lookup of the prior-reviewed SHA for a given PR:

1. `gh api repos/{owner}/{repo}/pulls/{n}/reviews` returns all reviews on the PR, including pending reviews authored by the authenticated user.
2. Filter to `user.login == $GITHUB_USER`.
3. Sort descending by `submitted_at`, falling back to `created_at` for pending reviews where `submitted_at` is null.
4. For each review in order, grep the body for `<!-- pr-review-agent:sha:([0-9a-f]{40}) -->`. The first match is the prior reviewed SHA.
5. If no review carries a sentinel, fall through to `state_read` (the phase-4 file).
6. If neither yields a SHA, this PR is a first-review case — full base..HEAD diff.

Multi-operator: each operator's daemon only sees its own login at step 2, so cross-operator sentinels never collide.

### Migration from the phase-4 state file

Phase-5 writes both sources and reads sentinel-first:

- `daemon/post-review.sh` appends the sentinel line to the review body before the `gh api` call. The existing `state_write` on success stays.
- `daemon/poll.sh` runs the sentinel discovery flow first. On hit, uses the sentinel SHA. On miss (API failure or no prior review with sentinel), falls back to `state_read`.
- After a phase of clean sentinel-reads in production, phase-6 (or later) removes the `state_write` call and the state-file fallback. The state-file write was already a placeholder for the `review_id` slot per `daemon/poll.sh:109`; the migration leaves the slot empty and tears out the rest at cutover.

The migration is reversible during phase-5: a sentinel-write regression is covered by the state-file fallback on the next tick without intervention.

## Consequences

| Property | Effect |
| --- | --- |
| Cross-machine continuity | Laptop reformat or daemon migration survives. Sentinel is on GitHub, not on disk. |
| Multi-operator coexistence | Each daemon greps its own `user.login` reviews; sentinels from other operators are ignored. |
| Diff-since-last-review base | The sentinel SHA is the base for `git diff <sentinel_sha>..HEAD` scoping — PRD #21's incremental review acceptance criterion. |
| Failure under `pending-conflict` ([ADR 0005](./0005-failure-handling-policy.md)) | Post fails, sentinel never lands, state file untouched. Next tick retries the same SHA naturally. |
| Failure under per-finding drops | Per-finding drops do not block the post; the review still ships and the sentinel still lands. |
| First-review case (no prior sentinel, no state) | Full base..HEAD diff, logged at info level so initial uptake is visible in the daemon log. |
| Sentinel parse failure | Treated as no sentinel found; fall through to state file. Logged at warn level so a sanitizer regression surfaces. |
| Sanitizer round-trip | A round-trip test posts a review with the sentinel, GETs the review back, and asserts the sentinel survives byte-exact. Pinned because the post pipeline is where a sanitizer regression would slip silently. |
| Backfill | Pre-phase-5 reviews carry no sentinel. The daemon treats them as if no prior review existed — full diff on first phase-5 tick. No retroactive sentinel-write. |

## Related

- [ADR 0001](./0001-architecture-baseline.md) D3 — pending-review-per-tick model; the sentinel travels in the same review body.
- [ADR 0003](./0003-identity-model.md) — operator identity and the review-body footer convention; the sentinel sits one line below the footer.
- [ADR 0005](./0005-failure-handling-policy.md) — system-vs-per-finding failure taxonomy; sentinel-write piggybacks on the existing post failure path with no new category.
- PRD #21 — V2 acceptance criteria including stateless markers and diff-since-last-review.
- `project_v1_v2_release_plan.md` — phase-5 work breakdown.
