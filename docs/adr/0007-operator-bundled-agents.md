# ADR 0007: Operator-bundled agent definitions for cross-repo review

Date: 2026-05-29
Status: Accepted

## Context

`claude -p "/review-pr ..."` runs with cwd set to the daemon's per-PR clone (the `$SCRATCH` directory in `review-pr.sh`), and claude loads slash commands + subagents from `cwd/.claude/`. Until now this forced target repos to carry `.claude/agents/review-agent-default.md` + `.claude/commands/review-pr.md` at PR HEAD. Only self-hosting worked out of the box.

Three deployments break under this constraint:

- Public release: operator installs pr-review-agent, points it at a target repo. Fails because target has no `.claude/`.
- CodeRabbit / Anthropic CR parity: install daemon-side only, register target, review. No target-repo modification.
- V2 reply path: same constraint for `review-agent-reply` + `reply-pr`.

The V2 PRD (issue #21) deferred this as issue #40 ("V3 cross-repo"). Wrong framing: it's the missing V1/V2 deployment piece, not a separate version.

## Decision

After clone + checkout of the per-PR scratch, the daemon copies its own bundled agent + slash-command files into `SCRATCH/.claude/`. Implemented as `bundle_operator_agents <scratch-dir>` in `daemon/lib.sh`, called from both `daemon/review-pr.sh` and `daemon/reply-pr.sh` between `git checkout` and `claude -p`:

- `<operator's pr-review-agent>/.claude/agents/review-agent-*.md` → `SCRATCH/.claude/agents/`
- `<...>/.claude/commands/review-pr.md` and `reply-pr.md` → `SCRATCH/.claude/commands/`

`cwd` for the claude process stays the per-PR clone so the agent's `Read` / `Glob` / `Grep` see target code. claude loads slash commands + subagents from `cwd/.claude/`, which the bundle just populated.

### Target-repo precedence

If a target-repo file already exists at the same path in the scratch (the repo ships its own `.claude/agents/review-agent-default.md`, for example), the bundle leaves it alone via `[[ -e dst ]] || cp src dst`. V2 does not support deliberate customization; the `extract_json.py` schema contract (ADR 0005) catches off-spec overrides as `category=schema-invalid`. Per-repo customization is deferred to a later ADR.

### Source location

The bundle reads from `$(dirname "${BASH_SOURCE[0]}")/..` of `lib.sh`, the pr-review-agent checkout that the daemon was invoked from. Install location auto-detects regardless of where pr-review-agent lives: macOS user paths, Linux user paths, `/opt/`, worktrees, external mounts.

## Consequences

- Cross-repo review works without target-repo setup. Add a repo to `.pr-review.yaml`, next polling cycle picks it up.
- Voice and style stay consistent across all watched repos; operator's prompt is the source of truth.
- Self-review path is unchanged: target == operator, files already exist in the clone, copy is a no-op.
- Operator's pr-review-agent install is load-bearing; uninstalling breaks all reviews (already true: daemon needs it to run).
- One `cp` step per polling cycle (~10ms, negligible). Updates to operator's `.claude/agents/*.md` take effect on the next polling cycle.
- Closes #40.

## Alternatives considered

- **`claude -p --plugin-dir "$PR_REVIEW_AGENT_ROOT"`**: would avoid the per-tick copy. Rejected after empirical testing on 2026-05-29: `--plugin-dir` requires a `.claude-plugin/plugin.json` manifest plus a top-level `commands/` + `agents/` layout (not `.claude/commands/` + `.claude/agents/`), and slash commands must be namespaced `/<plugin-name>:<cmd>`. Adopting that structure conflicts with pr-review-agent's self-hosted project-level layout. Restructuring as a first-class Claude Code Plugin is tracked in issue #42 for a later iteration.
- **Symlink `~/.claude/agents/review-agent-*.md` to operator's checkout during `install.sh`**: one-time install but pollutes operator's home and bleeds into any interactive `claude` session. Rejected for blast radius.
