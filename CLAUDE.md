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

Project identity (footer link + preview-release banner) is auto-derived from `git remote get-url origin` of the checkout. A normal `git clone` of any fork picks up the correct owner/repo with zero config — `taewanu/pr-review-agent`'s clone advertises itself; `myorg/my-fork`'s clone advertises itself.

Two env vars are honored as overrides for ad-hoc testing or non-git installs:

- `PR_REVIEW_PROJECT_URL` — overrides the derived repo URL
- `PR_REVIEW_PROJECT_NAME` — overrides the derived display name

```sh
PR_REVIEW_PROJECT_URL=https://github.com/some/fork \
PR_REVIEW_PROJECT_NAME=some-fork \
bash daemon/review-pr.sh <pr-url>
```

The daemon fails with an actionable error only if neither source yields values (rare: tarball install with no remote and no env vars).

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
