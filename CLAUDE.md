# pr-review-agent

Automated PR review tool running under the user's own GitHub identity. Daemon written in bash + Python, scheduled by macOS launchd, posts **pending** reviews via the `gh` CLI. Built on Claude Code subagents/skills/commands.

## Current state

- **Phase**: scaffolding complete, MVP not started
- **Toolchain**: bash + python (3.13 LTS) pinned via `mise.toml`
- **Lint/format/security**: pre-commit framework with ruff, shellcheck, shfmt, actionlint, gitleaks, yamllint
- **CI**: `.github/workflows/ci.yml` runs `pre-commit run --all-files`
- **Hosting**: not yet — GitHub repo will be created after MVP works locally

## Where to look

- Architectural decisions — `docs/adr/`
- Directory layout — the tree itself (`.claude/`, `daemon/`, `bin/`, `templates/`)
- Config example — `templates/.pr-review.example.yaml`

## Next session entry point

MVP backbone: implement the daemon (`daemon/*.sh`, `daemon/*.py`) and the orchestrating slash command (`.claude/commands/review-pr.md`). Use `to-issues` to break the work into vertical slices — do not implement horizontally. First slice candidate: "one PR dry-run end-to-end with one hardcoded persona, no Slack, no state file".

## Run commands

```sh
mise install
pre-commit install
pre-commit run --all-files
```

Daemon entry points (`daemon/poll.sh`, `bin/install.sh`) are stubs until MVP lands.
