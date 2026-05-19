# ADR 0001 — Architecture baseline

Date: 2026-05-19
Status: Accepted

Bundles four architectural decisions from the V1 design that are hard to reverse and warrant explicit rationale. Two further decisions (line-vs-position for inline comments, skip-own-PR) are excluded — they are trivially reversible and live in code-site comments instead.

## D1 — Polling instead of webhooks

### Context

PR review needs to react to "PR opened" and "new push" events on watched repositories. The two delivery mechanisms are GitHub webhooks and periodic polling.

### Decision

Poll watched repositories every 5 minutes from a local `launchd` job.

### Consequences

- No GitHub App registration, no public endpoint, no TLS termination, no inbound port exposure. Setup is one `bin/install.sh` away.
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

## D3 — Single pending review instead of individual comments

### Context

GitHub exposes both per-comment posting and grouped "review" objects. Reviews can be left `PENDING` so a human submits them. Per-comment posting lands immediately.

### Decision

Always post a single `PENDING` review per PR-tick, containing the summary as the review body and the inline findings as review comments.

### Consequences

- The author sees one cohesive review unit and accepts or dismisses it as a whole in the GitHub UI — pending-by-default is the noise-control mechanism.
- Re-runs replace the prior pending review for the same SHA rather than stacking duplicates.
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
