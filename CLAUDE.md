# pr-review-agent

Automated PR review tool running under the operator's own GitHub identity. Daemon written in bash + Python, scheduled by macOS `launchd`. Posts pending reviews via the `gh` CLI. Built on Claude Code subagents, skills, and slash commands.

## Run commands

Uses Python 3.13 + pre-commit 4 + ruff + shellcheck + shfmt + actionlint + gitleaks + yamllint via mise. Wrap commands with `mise exec --` so non-interactive shells (CI, git hooks) resolve the right binaries:

- `mise install` — install all pinned tools
- `mise exec -- pre-commit install` — register git hooks (one-time per clone)
- `mise exec -- pre-commit run --all-files` — run all hooks against tracked files

Pre-commit hooks run lint, format, and security checks on staged files. CI mirrors the same hooks via `.github/workflows/ci.yml`.

## Branching

Each slice ships as its own branch and PR into `main`. Squash merges. Conventional commit prefixes (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`).

## Where to look

- V1 scope and user stories — GitHub issue #1 (the PRD parent)
- Architectural decisions — `docs/adr/`
- Reviewer personas — `.claude/agents/pr-reviewer-*.md`
- Orchestrator slash command — `.claude/commands/review-pr.md`
- Daemon code — `daemon/`
- Setup scripts — `bin/`
- Template configs — `templates/`
- Vendored Pocock skills — `.claude/skills/` (MIT, see `CREDITS.md`)
