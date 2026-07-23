# ADR 0037: A lens earns its place only by what the base reviewer cannot do

Date: 2026-07-23
Status: Accepted

## Context

The review leg grew a lens set without an organizing principle. ADR 0023 shipped four domain lenses (`correctness`, `perf`, `security`, `tests`) alongside `default`, modeled on Anthropic's code-review skill's four fixed lenses. ADR 0035 added `intent`. The set was presented and dialed as flat peers (`REVIEW_LENSES=default correctness perf security tests intent`), which #222 and #250 flagged as hiding that they are not peers: `default` is a broad sweep, `correctness` was a second deeper read of the same ground, `intent` reads a different input, and the domain lenses narrow attention to one class.

Two facts about this repo's operation reframe the question:

- ADR 0034 measured the subagent harness redundant: single-agent mode matched the same five-lens set's recall at 27-39% fewer tokens by dropping a dispatch layer that only forwarded a subagent's stdout. It did not measure whether the lens count itself buys recall, since both modes ran five lenses. Separately, the eval corpus holds no perf, security, or test-quality defect (#249), though the full census is unrun, so that too is a recorded judgment rather than a measured result.
- Recall is unmeasurable at this PR volume (#185/#188/#218/#232 closed for it). Neither the value of a second read of the base's own ground nor the marginal recall of a domain lens can be settled by measurement here, so the set must be organized by a principle instead.

## Decision

A lens is justified only when it adds a capability the base reviewer (`default`) structurally cannot have. This sorts every lens into one of three tiers.

1. **Base reviewer (`default`).** Reads the diff and the surrounding code and hunts every class: correctness, data-flow (cross-component, caller-contract, co-varying-state, async/ordering), quality, and the shallow reach of the domain concerns. It is the review; the rest are justified only against it.

2. **Structural addition (`intent`).** Reads an input the base reviewer never sees: the PR's description and any linked issue, checked against the diff. No code-only lens can make that comparison, so it is non-redundant by construction. It defaults on and skips itself at zero cost when the change describes nothing (ADR 0035).

3. **Domain depth (`perf`, `security`, `tests`).** The base reviewer already reaches these classes; a domain lens only spends its whole budget reading one of them deeper. That is concentrated attention, not a new capability, so it earns its cost only where the domain is load-bearing for the repo under review. Off by default (#249); a fork where security or performance is the point turns it on in `.env`.

What this rejects: a lens that reads the same input as the base and hunts the base's own core class is a redundant read, not an addition. The `correctness` lens was exactly that, ADR 0022's redundancy lever. Three of its four categories were already in the default prompt; the fourth, async/ordering divergence, plus its depth instruction, were folded into `default` and the lens removed (ADR 0023 amended).

**Naming.** The base lens keeps the name `default`, not `code`. Every lens reads code, so `code` would not distinguish the base from the domain lenses; `default` names it for what it is, the reviewer that runs unless narrowed. This settles the rename #250 asked about: the axes are named by this three-tier principle, not by relabeling `default`.

> **Amended 2026-07-23 (`default` renamed `general`).** The paragraph above kept
> `default`, reading the name as "the reviewer that runs unless narrowed." That
> confuses two axes: `default` names the lens's *config role* (it is what
> `REVIEW_LENSES` falls back to), not its *job*. A name should reveal the job (Beck,
> intention-revealing names), and this lens's job is the broad generalist read: it
> hunts every class shallowly where the specialist lenses each go deep on one. So
> it is renamed `general` (`review-agent-general.md`, `LENS_LABELS[0]`, the unset
> default `general data-flow intent`). Its plumbing is unchanged and still
> unprefixed: the command stays `/review-pr`, the raw file stays
> `.pr-review-raw.txt`, and the ADR 0005 failure categories stay unprefixed, so
> `general` is the base in role exactly as `default` was. This reopens what #250
> asked, having found the earlier "principle over relabeling" answer wrong on the
> base's own name: naming it for its config role hid the job the tier exists to
> name.

> **Amended 2026-07-23 (data-flow re-added, a fourth tier).** The claim above that
> `correctness` was pure redundancy is corrected by measurement. A 3-fixture recall
> run (single-agent, the eval harness) found the base `default` sweep and the
> focused data-flow read catch *different* bugs, not the same one twice: the
> focused read caught a cross-component bug (sounds-abroad#165) that `default`
> missed even with the four data-flow categories and the depth instruction folded
> into its prompt, while `default` caught a local zero-division that the focused
> read drops as outside its scope. So the second read is a structural addition
> after all, of a different kind than `intent`: not a different input, but a
> different scope-and-depth on the same input, and the union catches what neither
> does alone. The lens returns, renamed `data-flow` for the role it plays; the
> `correctness` name overclaimed a general-correctness scope it never had (it only
> ever hunted four data-flow classes). It rejoins the default set (`default
> data-flow intent`) as a **fourth tier: a scope-focused specialist on the base's
> own input**, distinct from the opt-in domain lenses (perf/security/tests), which
> narrow to a domain the base only skims. n=3 is not a recall distribution, but the
> complementarity is structural (different scopes catch different classes), not a
> stochastic artifact. `default` keeps the folded async category and depth note: it
> still enumerates the four classes as candidates, `data-flow` reads them deep.

## Consequences

- Adding a lens now requires answering one question: what does the base reviewer structurally lack that this lens fills? A different input (like `intent`) can default on. Domain depth defaults off. A second read of the base's own ground is rejected outright.
- `REVIEW_LENSES` (ADR 0034) still lists the five lenses flat, but they are no longer conceived as peers: `default` is the review, `intent` is a standing addition, and `perf`/`security`/`tests` are opt-in depth. #219 (route cheap changes to fewer lenses) inherits this: a trivial diff drops the domain-depth tier and, where the change is self-describing, can drop `intent` too, never the base.
- The principle is a judgment frame, not a measurement result. It cannot prove the current set is optimal for recall, which is unmeasurable here; it makes the burden of proof explicit, so a lens is added or kept only when its structural contribution is nameable, and removed when it is only redundancy.
- This supersedes the "four domain lenses modeled on Anthropic's skill" framing that ADR 0023 gave as the set's rationale. That skill's four-lens model has no general sweep; this pipeline's `default` is the sweep, so its domain lenses are additions to a base, judged by this principle, rather than a fixed set copied wholesale.
