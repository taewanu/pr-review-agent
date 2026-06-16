# pr-review-agent

Local daemon that drafts PR reviews via Claude Code and posts them under the operator's own GitHub identity, gated by GitHub's pending-review state.

## Language

**Pending review**:
A GitHub PR Review object in the `PENDING` state. On **others' PRs** (author ≠ Operator) it is both the artifact and the safety gate: nothing is publicly visible until the Operator submits it. On the Operator's **own PRs** the gate is ceremony (you are reviewing your own code, and GitHub blocks self-APPROVE), so the daemon submits a COMMENT review directly instead. It lands immediately, and the Operator edits it after the fact (a submitted review cannot be deleted). One review per PR-tick either way. No separate "dry-run" mode exists.
_Avoid_: dry-run, draft review (the platform term is "pending"), preview

**Review body**:
The top-level summary text of a Pending review. 2–3 sentences, English. When some findings cannot be anchored to specific diff lines, the daemon appends an `## Findings outside the diff` section to the Review body (see ADR 0005).
_Avoid_: summary (use Review body for the formal term; "summary" is fine in casual prose)

**Finding**:
One logical review item the Review agent emits. Has a `path`, `line`, `severity` (`important` / `nit` / `pre_existing`), `type` (`bug` / `refactor` / `polish`), and `body`. Distinct from the Pending review's top-level Review body. Severity values render at posting time as 🔴 Important / 🟡 Nit / 🟣 Pre-existing per ADR 0002.
_Avoid_: comment (use Inline comment for the rendered API form), issue, remark

**Inline comment**:
A Finding after it has been rendered into GitHub's PR Review API shape: path + line + body with severity emoji prefix and bold type label. Each Finding either becomes an Inline comment (if anchored in the diff) or is relocated into the Review body's `## Findings outside the diff` section (if not).
_Avoid_: comment alone (ambiguous), finding (Finding is the logical unit; Inline comment is the rendered form)

**Review footer**:
The attribution-plus-next-action line closing a Review body, of the form `🤖 <verb> by [<Operator identity>](<project url>). <action>.`. Two variants on the pending/posted axis: on others' PRs the review is genuinely pending, so it reads "Drafted by … Submit, edit, or cancel as needed"; on the Operator's own PRs it is auto-submitted (ADR 0008), so it reads "Auto-submitted by … Edit as needed". Draft-status is stated here, once per review, and nowhere else. Format pinned by ADR 0010.
_Avoid_: AI-drafted footer (a posted Review body is not itself "drafted"; the old inline string is gone — see Provenance tag); banner (the preview-release banner is a separate, version-gated element above the body)

**Provenance tag**:
The compact `🤖 _pr-review-agent_` appended to every posted artifact that is not a Finding Review body (each Inline comment, each daemon reply, the Status comment, and the Reply review's disposition summary), answering "who wrote this" under the shared solo identity (ADR 0003). The split is by artifact level, not draft-status: the Finding Review body carries the Review footer; everything else carries the Provenance tag. It never encodes draft-status (that is the Review footer's job), so it is byte-identical on pending and posted artifacts. Generalizes the #82 reply fix into a rule. Format pinned by ADR 0010.
_Avoid_: AI-drafted (a posted item is not a draft; the old inline-finding string, removed in #87), marker (reserved for the hidden Sentinel / Status markers; the Provenance tag is visible and drives no dedup)

**Operator**:
The person whose `gh` CLI token the daemon uses. Reviews are authored under this identity. One operator per daemon installation.
_Avoid_: user (ambiguous with PR author), reviewer (GitHub's human-assigned PR reviewers; the Operator may or may not also be one)

**Review agent**:
A Claude Code subagent that reads a PR's diff and emits structured findings. Defined as a file at `.claude/agents/review-agent-*.md`. V1 ships one (`review-agent-default`); `security`, `perf`, and `tests` are vendored but inactive. Review agents are stateless and do not post; the daemon's posting step handles the GitHub side. Use the hyphenated form "review-agent" when the compound serves as a single token (identifiers, file names).
_Avoid_: agent alone (Claude Code's broader term for parallelization; use the qualified compound), reviewer (ambiguous with GitHub's human-assigned PR reviewers)

**Editor agent**:
A Claude Code subagent that refines the Review agent's output before it is posted. It re-reads the PR at HEAD independently, on a fresh context, so its judgment is not anchored to the author's reasoning (the bias it exists to remove); against that re-read it drops weak or inaccurate Findings, rewrites the surviving Finding bodies, and reconciles the Review body to match. Defined at `.claude/agents/review-agent-editor.md`. Stateless and does not post. Its levers are cut, reword, and reconcile only: it never changes a Finding's `severity` or `type` (the taxonomy is the daemon's per ADR 0002) or its `path`/`line` (anchoring owns relocation).
_Avoid_: reviewer (the Review agent emits Findings; the Editor refines them), critic (overloaded), persona (a wording profile within an agent, not a separate agent), draft (a posted review is not a draft; see Pending review)

**Persona**:
A review agent's identity profile = its Voice + Tone variation rules + Nuance patterns. Embedded in the agent's prompt. Personas shape how findings are *worded*; they do not affect schema fields (`severity`, `type`, `path`, `line`); those remain the daemon's responsibility per ADR 0002. Convention extends the Mailchimp/Polaris voice & tone model with an additional nuance layer.
_Avoid_: style (overloaded with visual/UI design)

**Voice**:
A review agent's fixed identity, "who it is." Constant across all findings in a review. V1 default voice: see `.claude/agents/review-agent-default.md` (Slack-style "X but never Y" pattern). Part of Persona.
_Avoid_: tone (voice is invariant; tone varies)

**Tone**:
How a review agent's voice shifts across review contexts (e.g., emphatic for `important` findings, light for `nit`, matter-of-fact for `pre_existing`). The voice stays constant; the tone adapts. Part of Persona. The first codified tone rule is the **severity floor**: a Review body's summary cannot read weaker than the highest-severity Finding it reports (an `important` Finding summarized as a minor aside is the undersell it forbids). The floor is one-directional, a minimum and not a target; finer tone variation still emerges from voice rather than being pinned.
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
A Finding plus its chain of replies; the unit `reply-pr.sh` processes. The same physical thread GitHub's GraphQL calls a *review thread* (the `PRRT_`-prefixed node `resolveReviewThread` acts on), and GitHub's web UI calls a *conversation* (the "Resolve conversation" control, the "Require conversation resolution" branch rule); the daemon keeps "thread" in code and GraphQL, and renders "conversation" in user-facing prose. It carries an open/resolved state on GitHub, see Thread resolution.

**Operator reply**:
A comment the Operator writes inside a Reply thread, in reply to a Finding.

**Reply agent**:
The Claude Code subagent `review-agent-reply`. Classifies each Operator reply into a Bucket and verifies a fix_claim against the file at HEAD. Stateless and does not post (the daemon posts). The `review-agent-*` prefix is the product-name namespace, not a "reviewing" claim.
_Avoid_: agent alone (see Review agent), "the agent replies" (the daemon posts, not the agent)

**Bucket**:
The Reply agent's classification of an Operator reply: `fix_claim`, `question`, or `acknowledgment`. `fix_claim` and `question` each earn a file read at HEAD and a text reply; `acknowledgment` is reaction-only.

**Reply mode**:
The Reply agent's verdict on a thread, posted by the daemon as a threaded text reply. Two value-sets, keyed by Bucket:

- `fix_claim` → `confirmed` / `pushback`. `confirmed` = the file at HEAD matches the Operator's claimed fix; `pushback` = the file still shows the mismatch, cited with file evidence.
- `question` → `stands` / `withdrawn`. `stands` = the Finding holds after re-examining the code at HEAD, with the reasoning given; `withdrawn` = the daemon concedes the Finding was wrong (a false positive) and retracts it.

All four verdicts are **daemon-authored**; the Operator only supplies the reply that triggers the verdict. The Operator never "pushes back" in this vocabulary: an Operator disputing a Finding ("why flag this?", "false positive") is the `question` Bucket, which the daemon answers `stands` or `withdrawn`, never pushback.
_Avoid_: pushback for an Operator's dispute of a Finding (that is the `question` Bucket; the daemon's reply to it is `stands`/`withdrawn`); rejection / denial (pushback cites file evidence, it does not deny intent); `confirmed`/`pushback` for a `question` verdict (those are `fix_claim`-only)

**Ack reaction**:
The reaction the daemon posts on an Operator reply (👀 for fix_claim/question, 👍 for acknowledgment): a user-facing acknowledgment, never a dedup signal (a reaction carries no author provenance the daemon can trust). For an `acknowledgment` it is the only ack, so its landing is guaranteed (retried until it POSTs, and the Reply sentinel is embedded only once it lands); for a `fix_claim` or `question` it is a light "seen" on top of the text reply.
_Avoid_: pickup reaction (old code term; "pickup" collides with the early-signal idea in #48), emoji (loses the typed, per-user, removable reaction semantics), ack alone (collides with the acknowledgment Bucket)

**Reply review**:
The single `COMMENT` review the daemon opens, fills, and submits once per reply-tick to wrap that tick's body-bearing reply acks (`fix_claim` + `question`), so the Operator gets **one** GitHub notification instead of one per thread (#38). Each ack still attaches to its own Reply thread (the `PRRT_` id), so threading and the per-reply Reply sentinel are unchanged; only the delivery is batched. Its Review body carries a one-line **disposition summary** (#11), then the Provenance tag, above the hidden reply-review marker (`pr-review-agent:reply-review`): the summary leads with the conversations still open (`pushback` / `stands`), then the count resolved (`confirmed` / `withdrawn`), over only the replies that landed in this review; the marker still lets the daemon tell its own stale wrapper apart from a Finding draft before deleting one. A reply whose thread id could not be read (degraded `reviewThreads` query, or a thread past the first-100 page) falls back to a detached `/replies` POST so it still lands, at the cost of its own notification. Mechanically a Pending review submitted as COMMENT, but always auto-submitted (replies were never gated), where the Finding-bearing Pending review can stay pending as a safety gate on others' PRs.
_Avoid_: Pending review (that is the Finding-bearing review the daemon drafts when reviewing a PR; a Reply review carries no Findings and no Sha sentinel, and its body is the one-line disposition summary, not a Finding Review body), batch comment (loses that it is a GitHub review object)

**Thread resolution**:
Marking a Reply thread `resolved` on GitHub via the GraphQL `resolveReviewThread` mutation, so the thread collapses out of the PR conversation. A *user-facing* state change that de-clutters the PR (the same effect as a human clicking "Resolve conversation"), driven by the daemon when **nothing actionable remains on the thread**. Two drivers:
- **Reply-driven** (#75): an Operator reply yields a Verdict of `confirmed` (the fix landed) or `withdrawn` (the Finding was retracted as a false positive). `pushback` and `stands` keep the Finding live, so they leave the thread open; `acknowledgment` carries no verdict and stays open.
- **Commit-driven** (ADR 0017): on a new HEAD SHA the daemon finds a Finding's flagged defect gone at HEAD and resolves with **no Operator reply**, posting a `_Fixed:_` note first. `_Fixed:_` is the commit-driven counterpart of the `confirmed` ack, kept distinct from the four reply Verdicts because no reply triggered it. Safe-biased: it resolves only on a positive per-thread judgment, else leaves the thread open.

Orthogonal to the Reply sentinel: resolution changes GitHub UI state, the sentinel records that a reply was *processed* — a thread can be processed-but-open (e.g. `pushback`) or, in principle, resolved-but-unprocessed (a human resolved it).
_Avoid_: addressed / done (the Reply sentinel already overloads "addressed"; resolution is strictly the GitHub open→resolved transition), close (GitHub "close" is a PR/issue action, distinct from resolving a thread), `_Fixed:_` as a Verdict (the four Verdicts answer an Operator reply; the commit-driven note answers a commit)

**review-dedup / reply-dedup**:
Concept labels (kebab) for the two dedup rules: "do not re-review the same SHA" (review-dedup, the Sha sentinel) and "do not re-process the same Operator reply" (reply-dedup, the Reply sentinel).

## Roadmap

**Version (`v0.2.2`, …)**:
A sequential release scope named by its semver: what's in scope when the project declares itself done for that release. The version lives in `pyproject.toml`, renders in the preview banner, and is cut as an annotated git tag `v0.A.B` whose planning milestone carries the same name; the bump lands in the tagged commit (ADR 0012). Early releases were displayed as V1, V2, V2.1 and cut as phase tags; the roadmap issue's release table maps them to semver. Architecturally divergent paths (Agent SDK headless mode in ADR 0001 D2, GitHub App identity in ADR 0003) are not a Version by themselves; they are decided in ADRs and land in whichever Version adopts them.
_Avoid_: V`A`.`B` display names like V2.2 (retired in ADR 0012; the semver is the one name); generation; phase (historical, see below)

**Phase (`phase-0`, …, `phase-6`)**:
Historical term: a themed band of work cut as an annotated git tag `phase-N`, the release markers before version-first naming (ADR 0012). Since `phase-4` each phase mapped 1:1 to a release; the roadmap issue's release table is the bridge from phase tags to semver. The tags stay; no new tag or milestone uses the name. Themed grouping inside a release is an "arc" (roadmap usage).
_Avoid_: naming new work phase-N; stage, iteration, sprint
