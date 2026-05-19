# ADR 0001 — Architecture baseline

Date: 2026-05-19
Status: Accepted

Bundles four architectural decisions from the V1 design — D1, D2, D3, and D5 from the original sequence — that are hard to reverse and warrant explicit rationale. D4 (line-vs-position for inline comments) is excluded as a trivially reversible API parameter choice and lives in code-site comments. The own-PR question, formerly D6, is captured in [ADR 0004](./0004-own-pr-review-default.md).

## D1 — Polling instead of webhooks

### Context

PR review needs to react to "PR opened" and "new push" events on watched repositories. The two delivery mechanisms are GitHub webhooks and periodic polling.

### Decision

Poll watched repositories every 5 minutes from a local `launchd` job.

### Consequences

- No GitHub App registration, no public endpoint, no TLS termination, no inbound port exposure. Setup is local with no external services to configure.
- Lag is bounded by the poll interval (~5 min). Acceptable for code review; not acceptable for chat-style features.
- API quota cost is modest: authenticated calls cap at 5000/hour, and a few `gh pr list` per repo per tick stays well inside that.
- Switching to webhooks later is non-trivial because the state model (per-PR SHA tracking in `state/`) assumes a pull-mode loop.

## D2 — Claude Code over Claude Agent SDK over Claude API

### Context

The reviewer process needs to read a checked-out PR, navigate files, and produce structured output. Three Anthropic-shaped options: (a) Claude Code CLI in `--print` mode, (b) Claude Agent SDK, (c) raw Claude API with custom tooling.

### Decision

Use Claude Code CLI invoked headlessly (`claude --print "/review-pr <url>"`).

### Consequences

- Subagents, skills, slash commands, and the file-system tool stack are already wired and battle-tested.
- A Claude Max subscription covers usage with no per-call billing.
- Tied to a macOS interactive-context assumption — Claude Code does not run headless on a server. A future "VPS/headless" variant would need to be rebuilt on the Agent SDK (tracked as V2 scope).
- Re-authentication is a manual step when the subscription token rotates; the daemon needs a clear failure path.
- Data crosses process boundaries as text — the CLI prints to stdout, the daemon captures it through bash variables and pipes, and a separate step extracts and validates the persona's structured output before posting. This LLM-produces-findings / deterministic-code-posts separation mirrors the architecture of both Anthropic Code Review and CodeRabbit and is required for automated daemon reliability: a slash-command body that ends with the `gh api` call gives no deterministic guarantee that posting actually happened.

## D3 — Single pending review instead of individual comments

### Context

GitHub exposes both per-comment posting and grouped "review" objects. Reviews can be left `PENDING` so a human submits them. Per-comment posting lands immediately.

### Decision

Always post a single `PENDING` review per PR-tick, containing the summary as the review body and the inline findings as review comments.

### Consequences

- The author sees one cohesive review unit and accepts or dismisses it as a whole in the GitHub UI — pending-by-default is the noise-control mechanism.
- The daemon's state file deduplicates by SHA to prevent re-posting on unchanged revisions. New commits produce a fresh pending review; any prior pending reviews remain for the operator to dismiss or submit.
- Per-comment posting (with its own rate-limit risks) is avoided.
- If a single comment in the bundle has a bad `line` value, GitHub rejects the whole review — the daemon needs a fallback path that converts bad inlines into the summary text.

## D5 — One installation watches N repositories

### Context

Users may want to monitor several repos from one machine. Options: one daemon per repo, or one daemon iterating over a configured list.

### Decision

Single daemon, repo list configured via the `REPOS` env var as a space-separated `owner/repo` list. Per-repo behavior is configured by the in-repo `.pr-review.yaml`.

### Consequences

- One `launchd` job, one log path, one state directory rooted at `state/<repo>/<pr>.json`.
- Per-repo overrides (persona selection, path filters, instructions) live in each repo's `.pr-review.yaml` — the daemon stays generic.
- Concurrent reviews are out of scope for V1; PRs are processed sequentially within a tick.
- Splitting into per-repo daemons later is a config-only change; the data model already namespaces state by repo.
