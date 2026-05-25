# pr-review-agent

Automated PR review tool running under the operator's own GitHub identity. Daemon written in bash + Python, scheduled by macOS `launchd`. Posts pending reviews via the `gh` CLI. Built on Claude Code subagents, skills, and slash commands.

## Run commands

Uses Python 3.13 + pre-commit 4 + ruff + shellcheck + shfmt + actionlint + gitleaks + yamllint via mise. Wrap commands with `mise exec --` so non-interactive shells (CI, git hooks) resolve the right binaries:

- `mise install` — install all pinned tools
- `mise exec -- pre-commit install` — register git hooks (one-time per clone)
- `mise exec -- pre-commit run --all-files` — run all hooks against tracked files

Pre-commit hooks run lint, format, and security checks on staged files. CI mirrors the same hooks via `.github/workflows/ci.yml`.

## Run daemon

Slice 1 ships the end-to-end pipeline as a manual one-shot:

```bash
bash daemon/review-pr.sh <pr-url>
```

Prereqs: `gh auth login` (operator identity per ADR 0003), `claude` on PATH, `jq`, `python3`. The daemon preflights and bails with an actionable hint if any are missing.

Polling, `launchd`, and the install wizard are deferred to Phase 3+. V1 targets own-repo PRs only (ADR 0004).

## Forking

Two env vars are **required**. The daemon refuses to start without them so a fork can never silently advertise the upstream project in its own PRs:

- `PR_REVIEW_PROJECT_URL` — repo URL surfaced in the Review-body footer link and the preview-release banner
- `PR_REVIEW_PROJECT_NAME` — display name used in the same two places

Setup (once per clone):

```sh
cp .env.example .env
# Edit .env to fill in your values
```

`.env` is gitignored and auto-loaded from the repo root by both `daemon/review-pr.sh` and `daemon/post-review.sh` before any other work. Shell env wins over `.env`, so for one-off overrides (testing a fork's branding without editing the file):

```sh
PR_REVIEW_PROJECT_URL=https://github.com/some/fork \
PR_REVIEW_PROJECT_NAME=some-fork \
bash daemon/review-pr.sh <pr-url>
```

Tests disable `.env` loading via `PR_REVIEW_ENV_FILE=/dev/null` so a developer's local config can't contaminate snapshot fixtures.

Per-repo `.pr-review.yaml` overrides are deferred until config loading lands.

## Branching

Each slice ships as its own branch and PR into `main`. Squash merges. Conventional commit prefixes (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`).

## Where to look

- V1 scope and user stories — GitHub issue #1 (the PRD parent)
- Architectural decisions — `docs/adr/`
- Review agents — `.claude/agents/review-agent-*.md`
- Orchestrator slash command — `.claude/commands/review-pr.md`
- Daemon code — `daemon/`
- Setup scripts — `bin/`
- Template configs — `templates/`
- Vendored Pocock skills — `.claude/skills/` (MIT, see `CREDITS.md`)
