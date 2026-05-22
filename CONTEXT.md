# pr-review-agent

Local daemon that drafts PR reviews via Claude Code and posts them under the operator's own GitHub identity, gated by GitHub's pending-review state.

## Language

**Pending review**:
A GitHub PR Review object in the `PENDING` state. The daemon's single posting unit: one Pending review per PR-tick, containing a Review body plus inline comments. Nothing is publicly visible on the PR until the operator submits, cancels, or edits it in the GitHub UI. Acts as both the artifact and the safety gate — no separate "dry-run" mode exists.
_Avoid_: dry-run, draft review (the platform term is "pending"), preview

**Review body**:
The top-level summary text of a Pending review. 2–3 sentences, English. When some findings cannot be anchored to specific diff lines, the daemon appends an `## Additional findings` section to the Review body (see ADR 0005).
_Avoid_: summary (use Review body for the formal term; "summary" is fine in casual prose)

**Finding**:
One logical review item the Review agent emits. Has a `path`, `line`, `severity` (`important` / `nit` / `pre_existing`), `type` (`bug` / `refactor` / `polish`), and `body`. Distinct from the Pending review's top-level Review body. Severity values render at posting time as 🔴 Important / 🟡 Nit / 🟣 Pre-existing per ADR 0002.
_Avoid_: comment (use Inline comment for the rendered API form), issue, remark

**Inline comment**:
A Finding after it has been rendered into GitHub's PR Review API shape — path + line + body with severity emoji prefix and bold type label. Each Finding either becomes an Inline comment (if anchored in the diff) or is relocated into the Review body's `## Additional findings` section (if not).
_Avoid_: comment alone (ambiguous), finding (Finding is the logical unit; Inline comment is the rendered form)

**Operator**:
The person whose `gh` CLI token the daemon uses. Reviews are authored under this identity. One operator per daemon installation.
_Avoid_: user (ambiguous with PR author), reviewer (GitHub's human-assigned PR reviewers — the Operator may or may not also be one)

**Review agent**:
A Claude Code subagent that reads a PR's diff and emits structured findings. Defined as a file at `.claude/agents/review-agent-*.md`. V1 ships one (`review-agent-default`); `security`, `perf`, and `tests` are vendored but inactive. Review agents are stateless and do not post — the daemon's posting step handles the GitHub side. Use the hyphenated form "review-agent" when the compound serves as a single token (identifiers, file names).
_Avoid_: agent alone (Claude Code's broader term for parallelization — use the qualified compound), reviewer (ambiguous with GitHub's human-assigned PR reviewers)

**Persona**:
A review agent's identity profile = its Voice + Tone variation rules + Nuance patterns. Embedded in the agent's prompt. Personas shape how findings are *worded*; they do not affect schema fields (`severity`, `type`, `path`, `line`) — those remain the daemon's responsibility per ADR 0002. Convention extends the Mailchimp/Polaris voice & tone model with an additional nuance layer.
_Avoid_: style (overloaded with visual/UI design)

**Voice**:
A review agent's fixed identity — "who it is." Constant across all findings in a review. V1 default voice: see `.claude/agents/review-agent-default.md` (Slack-style "X but never Y" pattern). Part of Persona.
_Avoid_: tone (voice is invariant; tone varies)

**Tone**:
How a review agent's voice shifts across review contexts (e.g., emphatic for `important` findings, light for `nit`, matter-of-fact for `pre_existing`). The voice stays constant; the tone adapts. Part of Persona. Tone variation rules are not codified in V1 — they emerge from voice naturally and may be pinned in Phase 3+.
_Avoid_: voice (tone varies; voice is invariant), nuance (tone is the category-level shift; nuance is finer)

**Nuance**:
Micro-variation within a given tone — word choice, sentence endings, rhythm, emphasis position. The finest-grained layer of the character profile. Part of Persona. Not codified in V1 — emerges from voice and tone naturally.
_Avoid_: tone (nuance is a sub-layer within a tone)

**PR-tick**:
One iteration of the poll loop's work on a single PR. Produces at most one new Pending review (skipped if HEAD SHA matches the last-reviewed SHA in state).
_Avoid_: run, cycle, pass

**Scratch directory**:
A short-lived working tree the daemon creates per PR-tick, holding a shallow clone checked out to the PR's HEAD. Claude Code runs with the scratch directory as its cwd. Deleted at the end of the PR-tick.
_Avoid_: workspace, sandbox, checkout dir

**`.pr-review.yaml`**:
The per-repo configuration file the daemon reads on each PR-tick. Single file; carries structured fields (path filters, max findings, active review agents) plus a multiline `instructions:` field for prose review guidance. Lives at the repo root, committed alongside source.
_Avoid_: REVIEW.md (Anthropic Code Review's pattern uses a separate markdown file; ours bundles prose into the YAML `instructions:` field)

## Roadmap

**Version (V1, V2, …)**:
A release marker — what's in scope when the project declares itself done for that release. V1's scope is GitHub issue #1 (the PRD). V2 is a placeholder for architecturally divergent paths (e.g., Agent SDK headless mode in ADR 0001 D2, GitHub App identity in ADR 0003).
_Avoid_: milestone, generation

**Phase (Phase 0, Phase 1, …)**:
A delivery slice on the path to a Version. One Phase = one tracer-bullet vertical slice that ships as a single PR into `main`. Multiple Phases compose into a Version. A Phase is not a time window, not a team, and not a milestone — it's the unit of shippable work.
_Avoid_: stage, milestone, iteration, sprint
