# pr-review-agent

Automated PR review tool that runs under your own GitHub identity. Built on Claude Code's subagents, skills, and slash commands. Reviews land as a **pending** review so you stay in control of what actually gets submitted.

> Status: **scaffolding** — V1 not yet usable. See `docs/adr/` for architectural decisions.

## Why this exists

- Review under your own GitHub identity, not a bot account
- Multiple review agents per teammate; teammates' daemons can each review the same PR
- Runs on your own machine via `launchd` (macOS); no webhooks, no GitHub App registration, no external hosting
- MIT licensed — fork and shape it to your team

## Install

TBD (V1 in progress).

## License

[MIT](./LICENSE)
