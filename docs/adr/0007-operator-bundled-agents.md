# ADR 0007 — Operator-bundled agent definitions for cross-repo review

Date: 2026-05-29
Status: Accepted

## Context

`claude -p "/review-pr ..."` runs with cwd set to the daemon's per-PR clone (the `$SCRATCH` directory in `review-pr.sh`), and claude loads slash commands + subagents from `cwd/.claude/`. Until now this forced target repos to carry `.claude/agents/review-agent-default.md` + `.claude/commands/review-pr.md` at PR HEAD. Only self-hosting worked out of the box.

Three deployments break under this constraint:

- Public release: operator installs pr-review-agent, points it at a target repo. Fails because target has no `.claude/`.
- CodeRabbit / Anthropic CR parity: install daemon-side only, register target, review. No target-repo modification.
- V2 reply path: same constraint for `review-agent-reply` + `reply-pr`.

PRD #21 deferred this as issue #40 ("V3 cross-repo"). Wrong framing: it's the missing V1/V2 deployment piece, not a separate version.

## Decision

The daemon passes `--plugin-dir "$PR_REVIEW_AGENT_ROOT"` to `claude -p`. Claude's plugin loader reads `<plugin-dir>/.claude/commands/` and `<plugin-dir>/.claude/agents/` and registers the slash commands + subagents for the session, regardless of cwd. The existing `.claude/` layout is the plugin contract; no `plugin.json` manifest required.

`daemon/review-pr.sh` and `daemon/reply-pr.sh` define:

    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    PR_REVIEW_AGENT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

and pass `--plugin-dir "$PR_REVIEW_AGENT_ROOT"` to their `claude -p` invocations. cwd stays the per-PR clone so the agent's `Read` / `Glob` / `Grep` see target code.

### Note on target-repo loading

Claude's `cwd > --plugin-dir` precedence (verified 2026-05-29) means a target repo's own `.claude/agents/review-agent-default.md` overrides the operator's bundle. V2 does not support customization; the `extract-json.py` schema contract (ADR 0005) catches off-spec overrides as `category=schema-invalid`. Per-repo customization is deferred to a later ADR.

### Source location

`$PR_REVIEW_AGENT_ROOT` derives from `$0`, auto-detecting any install location: macOS user paths, Linux user paths, `/opt/`, worktrees, external mounts.

## Consequences

- Cross-repo review works without target-repo setup. Add a repo to `.pr-review.yaml`, next polling cycle picks it up.
- Voice and style stay consistent across all watched repos.
- Self-review path unchanged: target == operator, cwd-resolved files win, plugin-dir is harmless duplication.
- Operator's pr-review-agent install is load-bearing; uninstalling breaks all reviews (already true: daemon needs it to run).
- No file copying per polling cycle; agent updates take effect on the next claude invocation.
- Closes #40.

## Alternatives considered

- **Copy operator's `.claude/` into the scratch before `claude -p`**: extra `cp` step + bash precedence logic per polling cycle. Rejected: `--plugin-dir` does the same as a built-in flag with claude owning precedence.
- **Symlink `~/.claude/agents/review-agent-*.md` to operator's checkout during `install.sh`**: pollutes operator's home and bleeds into any interactive `claude` session. Rejected for blast radius.
