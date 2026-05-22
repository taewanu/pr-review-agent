# pr-review-agent

Automated PR review tool that runs under your own GitHub identity. Built on Claude Code's subagents, skills, and slash commands. Reviews land as a **pending** review so you stay in control of what actually gets submitted.

> Status: **scaffolding** — V1 not yet usable. See `docs/adr/` for architectural decisions.

## Why this exists

- Review under your own GitHub identity, not a bot account
- Multiple review agents per teammate; teammates' daemons can each review the same PR
- Runs on your own machine via `launchd` (macOS); no webhooks, no GitHub App registration, no external hosting
- MIT licensed — fork and shape it to your team

## Prerequisites

- `gh` CLI, authenticated (`gh auth login`) — operator identity per [ADR 0003](docs/adr/0003-identity-model.md)
- `claude` CLI on PATH
- `git`, `jq`, `python3` (3.13+)
- [mise](https://mise.jdx.dev/) for the pinned dev toolchain

## Run

Phase 2 Slice 1 ships the end-to-end pipeline as a manual one-shot:

```bash
bash daemon/review-pr.sh <pr-url>
```

V1 targets PRs in the operator's own repos (per [ADR 0004](docs/adr/0004-own-pr-review-default.md)) — the scratch clone needs `.claude/agents/review-agent-default.md` and `.claude/commands/review-pr.md` at PR HEAD, so cross-repo support is V2+ territory. Polling, `launchd`, and the install wizard land in later Phase 2/3 slices.

## Install

TBD (Phase 3+ install wizard).

## License

[MIT](./LICENSE)
