# pr-review-agent

Automated PR review tool that posts as a self-hosted GitHub App (ADR 0036). Daemon written in bash + Python, run as a foreground polling loop and optionally installed as a background `launchd` job (ADR 0011). Submits reviews via the `gh` CLI under the App's `[bot]` identity. Built on `claude -p`: one orchestrator session spawns the generator roles as parallel subagents, and the editor, judge-fix, and reply passes run as directly-prompted processes (ADR 0038 as amended).

## Run commands

Uses Python 3.13 + pre-commit 4 + ruff + shellcheck + shfmt + actionlint + gitleaks + yamllint via mise. Wrap commands with `mise exec --` so non-interactive shells (CI, git hooks) resolve the right binaries:

- `mise install` — install all pinned tools
- `mise exec -- pre-commit install` — register git hooks (one-time per clone)
- `mise exec -- pre-commit run --all-files` — run all hooks against tracked files

Pre-commit hooks run lint, format, and security checks on staged files. CI mirrors the same hooks via `.github/workflows/ci.yml`.

## Run daemon

The daemon is the `daemon/run.sh` polling loop (ADR 0009): each cycle drives `daemon/poll.sh`, which reviews the watched repos. Two ways to run it.

**Foreground** (primary, per ADR 0011): `bash daemon/run.sh`. Progress prints to the terminal (`polling…`, `reviewing…`, `tick done`); Ctrl-C stops it cleanly (the `INT`/`TERM` trap releases the pidfile singleton). Update with Ctrl-C → `git pull` → re-run — a pull that only touches `poll.sh` or the review pipeline is picked up on the next tick without a restart, since `run.sh` re-invokes `bash poll.sh` each cycle. `POLL_INTERVAL_SECONDS` (env wins, then `.env`, default 300) sets the cycle; set it in the shell for a short debug loop.

**Background, optional** (always-on across logout/reboot): `bash bin/install.sh` registers a `KeepAlive` launchd job running the same loop (supervising the process rather than firing a `StartInterval` timer, which stalled silently across sleep/wake, #83); stop with `bash bin/uninstall.sh`. Logs flow to `.daemon.log`; liveness: `echo $(( $(date +%s) - $(cat ~/.pr-review-agent/daemon.heartbeat) ))s since last cycle`. Caveat (ADR 0011): the launchd job is invisible and bound to this checkout's working tree, so keep that checkout on `main` — switching its branch silently breaks the running daemon. If you also develop here, run the dogfood daemon in the foreground or from a separate clone.

**Manual one-shot** (debugging or single-PR runs): `bash daemon/review-pr.sh <pr-url>` runs the review pipeline once without polling. `bash daemon/reply-pr.sh <pr-url>` runs the operator-reply ack pass once without polling. Reviews submit immediately under the App identity (ADR 0036), so there is no separate submit step.

Prereqs: a registered GitHub App the daemon authenticates as (its id in `GITHUB_APP_ID`, its private key at `~/.pr-review-agent/app.pem`), so no `gh auth login` (ADR 0036); `gh`, `claude`, `openssl`, `curl` on PATH; `jq`; `python3` 3.13+. Scripts preflight and bail with an actionable hint if any are missing. README's "Register the GitHub App" is the registration walkthrough: permissions (`contents: write` buys thread resolution alone), webhook off, key at mode 0600, install per watched repo.

The daemon bundles its own agent definitions into the scratch clone before invoking `claude -p` (per ADR 0007), so target repos do **not** need to carry `.claude/agents/`. A target repo can override by carrying its own file at the same path; the bundle's `[[ -e dst ]] || cp` guard preserves the existing file.

## Forking

The review footer names the App the daemon posts as, linking to its page (`github.com/apps/<slug>`). The slug comes from the installation probe's `app_slug` (`render_review_footer` in `daemon/lib.sh`), so attribution follows the App you register and install (ADR 0036), not the clone's git remote. A fork attributes to its own App once `GITHUB_APP_ID` and the private key are set.

## Branching

Each change ships as its own branch and PR into `main`. `main` is protected: deletion blocked, force-push blocked, linear history required, PR review threads must be resolved, status checks `lint` + `test` enforced, squash-only. Conventional commit prefixes (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`).

## Where to look

- V1 shipped scope — `phase-4` annotated tag (Added/Fixed/Trip-ups manifest); original PRD at issue #1 (closed)
- V2 shipped scope — `phase-5` annotated tag; V2 PRD at issue #21 (closed)
- Architectural decisions — `docs/adr/`
- Review agents — `.claude/agents/review-agent-*.md`
- Daemon code — `daemon/`
- Setup scripts — `bin/`
- Template configs — `templates/`
- Vendored Pocock skills — `.claude/skills/` (MIT, see `CREDITS.md`)
