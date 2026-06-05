# pr-review-agent

Local daemon that drafts PR reviews via Claude Code and posts them under the operator's own GitHub identity, gated by GitHub's pending-review state.

## Language

**Pending review**:
A GitHub PR Review object in the `PENDING` state. On **others' PRs** (author ≠ Operator) it is both the artifact and the safety gate: nothing is publicly visible until the Operator submits it. On the Operator's **own PRs** the gate is ceremony (you are reviewing your own code, and GitHub blocks self-APPROVE), so the daemon submits a COMMENT review directly instead. It lands immediately, and the Operator edits it after the fact (a submitted review cannot be deleted). One review per PR-tick either way. No separate "dry-run" mode exists.
_Avoid_: dry-run, draft review (the platform term is "pending"), preview

**Review body**:
The top-level summary text of a Pending review. 2–3 sentences, English. When some findings cannot be anchored to specific diff lines, the daemon appends an `## Additional findings` section to the Review body (see ADR 0005).
_Avoid_: summary (use Review body for the formal term; "summary" is fine in casual prose)

**Finding**:
One logical review item the Review agent emits. Has a `path`, `line`, `severity` (`important` / `nit` / `pre_existing`), `type` (`bug` / `refactor` / `polish`), and `body`. Distinct from the Pending review's top-level Review body. Severity values render at posting time as 🔴 Important / 🟡 Nit / 🟣 Pre-existing per ADR 0002.
_Avoid_: comment (use Inline comment for the rendered API form), issue, remark

**Inline comment**:
A Finding after it has been rendered into GitHub's PR Review API shape: path + line + body with severity emoji prefix and bold type label. Each Finding either becomes an Inline comment (if anchored in the diff) or is relocated into the Review body's `## Additional findings` section (if not).
_Avoid_: comment alone (ambiguous), finding (Finding is the logical unit; Inline comment is the rendered form)

**Operator**:
The person whose `gh` CLI token the daemon uses. Reviews are authored under this identity. One operator per daemon installation.
_Avoid_: user (ambiguous with PR author), reviewer (GitHub's human-assigned PR reviewers; the Operator may or may not also be one)

**Review agent**:
A Claude Code subagent that reads a PR's diff and emits structured findings. Defined as a file at `.claude/agents/review-agent-*.md`. V1 ships one (`review-agent-default`); `security`, `perf`, and `tests` are vendored but inactive. Review agents are stateless and do not post; the daemon's posting step handles the GitHub side. Use the hyphenated form "review-agent" when the compound serves as a single token (identifiers, file names).
_Avoid_: agent alone (Claude Code's broader term for parallelization; use the qualified compound), reviewer (ambiguous with GitHub's human-assigned PR reviewers)

**Persona**:
A review agent's identity profile = its Voice + Tone variation rules + Nuance patterns. Embedded in the agent's prompt. Personas shape how findings are *worded*; they do not affect schema fields (`severity`, `type`, `path`, `line`); those remain the daemon's responsibility per ADR 0002. Convention extends the Mailchimp/Polaris voice & tone model with an additional nuance layer.
_Avoid_: style (overloaded with visual/UI design)

**Voice**:
A review agent's fixed identity, "who it is." Constant across all findings in a review. V1 default voice: see `.claude/agents/review-agent-default.md` (Slack-style "X but never Y" pattern). Part of Persona.
_Avoid_: tone (voice is invariant; tone varies)

**Tone**:
How a review agent's voice shifts across review contexts (e.g., emphatic for `important` findings, light for `nit`, matter-of-fact for `pre_existing`). The voice stays constant; the tone adapts. Part of Persona. Tone variation rules are not codified in V1; they emerge from voice naturally and may be pinned in Phase 3+.
_Avoid_: voice (tone varies; voice is invariant), nuance (tone is the category-level shift; nuance is finer)

**Nuance**:
Micro-variation within a given tone: word choice, sentence endings, rhythm, emphasis position. The finest-grained layer of the character profile. Part of Persona. Not codified in V1; emerges from voice and tone naturally.
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

**Status comment**:
A pr-review-agent-owned issue comment on the PR, edited in place, showing the review scope (#60). One per PR, mutable. Identified by the Status marker so a re-review edits it rather than posting a second. Carries scope only, never findings (those live in the Review object).
_Avoid_: ACK comment, review (it is not a Review object), status check (a GitHub commit status, unrelated)

**Sentinel**:
Umbrella term for the hidden HTML-comment markers that drive dedup. A Sentinel is invisible in rendered markdown and lives in a comment body pr-review-agent owns. Two kinds: the Sha sentinel and the Reply sentinel.
_Avoid_: marker (broader; the Status marker is not a Sentinel), tag

**Sha sentinel**:
`pr-review-agent:sha:<SHA>` embedded in the Review body. Drives review-dedup: the daemon skips a PR-tick whose HEAD SHA already carries one (ADR 0006).

**Reply sentinel**:
`pr-review-agent:reply:<reply-id>`, where `<reply-id>` is an Operator reply's comment id. Drives reply-dedup so a processed reply is not re-dispatched (#39, #79). Two carriers: a fix_claim's threaded text reply, or, for a non-claim thread, the parent Finding's Inline comment body.
_Avoid_: addressed sentinel (implies the finding was resolved; the marker only means pr-review-agent processed the reply)

**Status marker**:
`pr-review-agent:status`. Identifies the Status comment for edit-in-place reuse. Not a Sentinel: it drives no dedup (a lib.sh invariant).

**Reply thread**:
A Finding plus its chain of replies; the unit `reply-pr.sh` processes.

**Operator reply**:
A comment the Operator writes inside a Reply thread, in reply to a Finding.

**Reply agent**:
The Claude Code subagent `review-agent-reply`. Classifies each Operator reply into a Bucket and verifies a fix_claim against the file at HEAD. Stateless and does not post (the daemon posts). The `review-agent-*` prefix is the product-name namespace, not a "reviewing" claim.
_Avoid_: agent alone (see Review agent), "the agent replies" (the daemon posts, not the agent)

**Bucket**:
The Reply agent's classification of an Operator reply: `fix_claim`, `question`, or `acknowledgment`. Only `fix_claim` earns a file read and a text reply; the other two are reaction-only.

**Ack reaction**:
The reaction the daemon posts on an Operator reply (👀 for fix_claim/question, 👍 for acknowledgment): a user-facing acknowledgment, never a dedup signal (a reaction carries no author provenance the daemon can trust). For a non-claim thread it is the only ack, so its landing is guaranteed (retried until it POSTs, and the Reply sentinel is embedded only once it lands); for a fix_claim it is a light "seen" on top of the text reply.
_Avoid_: pickup reaction (old code term; "pickup" collides with the early-signal idea in #48), emoji (loses the typed, per-user, removable reaction semantics), ack alone (collides with the acknowledgment Bucket)

**review-dedup / reply-dedup**:
Concept labels (kebab) for the two dedup rules: "do not re-review the same SHA" (review-dedup, the Sha sentinel) and "do not re-process the same Operator reply" (reply-dedup, the Reply sentinel).

## Roadmap

**Version (V1, V2, V2.1, …)**:
A sequential release scope: what's in scope when the project declares itself done for that release. V1 = the original PRD (issue #1). V2 = iterative review mode (dedup, threading, confirmation; PRD #21). Point releases (V2.1, …) carry follow-on refinements. Architecturally divergent paths (Agent SDK headless mode in ADR 0001 D2, GitHub App identity in ADR 0003) are not a Version by themselves; they are decided in ADRs and land in whichever Version adopts them.
_Avoid_: generation; milestone (a GitHub milestone tracks a Phase, not a Version); reserving V2 as "the architecture-divergence bucket" (an earlier, abandoned framing)

**Phase (`phase-0`, `phase-1`, …)**:
A themed band of work on the path to a Version, tracked as a GitHub milestone and closed with an annotated git tag `phase-N`. A Phase bundles the several PRs that share one theme (e.g. `phase-6` = operator-identity UX consequences). It is not a single PR and not a time window. Multiple Phases compose into a Version.
_Avoid_: stage, iteration, sprint; equating a Phase with a single PR (an earlier, abandoned definition)
