# ADR 0016: Post-generation editorial pass over review content

Date: 2026-06-12
Status: Accepted

## Context

The product differentiator is the quality of what a review *says*, not just how it is formatted. The format layer is settled (ADR 0010), but nothing improves the content of a Finding once the Review agent emits it: the daemon schema-validates the payload, runs the lexical voice gate, anchors, and posts. A weak Finding (inaccurate, vague, unsupported, or thinly argued) flows straight through.

Two prior decisions bound how this can be fixed.

- **The author prompt has a ceiling.** `review-agent-default.md` was iterated repeatedly during early verification; each pass improved clarity and economy but never moved the load-bearing semantic properties (the prompt could not make the agent reliably lead with the point or stop reaching for an em dash). The lesson, recorded in `project_voice_iteration_limits` and ADR 0010 §4, is that an author generating text from scratch resists prompt correction on judgment-level properties.
- **The deterministic gate cannot judge content.** `voice.py` (ADR 0010 §4) enforces only lexical and structural rules: a forbidden opener word, the em-dash character, a task-scoped ref, the 2 to 4 bullet count. It deliberately does not enforce semantic shape, because a post-hoc regex can only false-positive on a judgment ("this reasoning is multi-point, so it must be bulleted" is wrong for a legitimate single-point body).

So the content problem cannot be solved by iterating the author prompt further, nor by extending `voice.py`. What is left is a second judgment, made by something that is not the author.

## Considered options

- **Iterate the Review agent prompt harder (rejected).** This is the path the ceiling already closed. More examples and harder constraints buy clarity, not accuracy or argumentation. The author is anchored to its own reasoning; it cannot reliably critique the draft it just produced.
- **Extend `voice.py` to score content (rejected).** Content quality is a judgment, and ADR 0010 §4 fixed the boundary that keeps judgments out of the deterministic gate precisely because they false-positive. A regex cannot tell an unsupported claim from a terse correct one.
- **Run the editor as a subagent inside the existing `/review-pr` call (rejected).** Cheaper (one `claude -p`, no second cold start) and it would keep the voice gate where it sits. Rejected for fit: the daemon's architecture is discrete, observable, independently testable stages (ADR 0001), and the recent architecture work pushes further that way. Folding a second agent into one opaque call bundles both under one timeout, one log, one token figure, and makes the editor un-testable in isolation.
- **Degrade to the author's review when the editor fails (rejected).** Robust ("never lose a review"), but it forces the voice gate to run in two places so the fallback text is still validated, and it keeps the full author path alive as a second posting route. Rejected for the simpler fail-closed model below, which the daemon already uses everywhere else.
- **Let the editor re-judge `severity` and `type` (rejected).** Tempting for an over-labeled Finding, but it hands the taxonomy to the editor and blurs who owns severity. ADR 0002 makes the taxonomy the daemon's. The over-label case is already covered by the editor's drop lever; the rarer under-label case is low-harm. Left as a possible future issue if dogfooding surfaces cases that drop cannot handle.
- **Have the editor re-emit every surviving body (rejected after the trial).** A simpler output contract (the Editor returns a full payload like the author), but the trial pass showed an LLM intermittently HTML-escapes a `<` or writes a literal `\n` when reserializing text, corrupting a body it did not even change. Carrying kept bodies by reference removes that exposure on the common path, and the decisions-keyed output is also smaller and cheaper; the fidelity check backstops the rewrite path, where re-emission is unavoidable.

## Decision

Add a fresh **Editor agent** (`review-agent-editor`, see CONTEXT.md) as a daemon stage that runs after the Review agent and before posting.

1. **Fresh and independent.** The Editor runs in its own context as a second `claude -p` stage, so it never sees the author's reasoning, only the author's output. It re-reads the PR at HEAD itself rather than trusting each Finding's claim, making it a second independent judgment constrained to the author's Finding set. This is why it escapes the author's prompt ceiling: reacting to a concrete draft against the real code is a different, easier task than generating the draft.

2. **Three levers, bounded surface.** The Editor may drop a weak or inaccurate Finding, rewrite a surviving Finding's body, and reconcile the Review body to the surviving set. It may not change a Finding's `severity` or `type` (ADR 0002 owns the taxonomy) or its `path` or `line` (`anchor_findings.py` owns relocation). An over-labeled Finding is dropped, not relabeled.

   The Editor expresses these as a decision per input Finding (keep, drop, or rewrite with a new body), keyed by the Finding's index; the daemon applies the decisions to the author's payload. A kept Finding carries the author's original body by reference, not re-emitted text, so the common path cannot corrupt a body the Editor never changed. A surviving body that itself breaks the voice rules is a required micro-rewrite, not a keep.

3. **The voice gate moves behind the Editor, and gains a fidelity check.** Because the Editor rewrites the text that actually ships, the lexical voice checks must validate *its* output, not the author's. `voice.py` keeps its existing rules (lexical and structural only, ADR 0010 §4); its position moves from inside the author's extraction to after the Editor, and it gains one deterministic check: a body must not HTML-entity-escape `<`, `>`, or `&`, nor carry a literal `\n` in place of a newline. Those are mechanical corruptions an LLM occasionally introduces when reserializing text, invisible to the lexical voice rules but losslessly checkable, so they belong in the gate under ADR 0010 §4's lexical-or-structural remit, not left to the prompt. The author's payload is schema-validated to hand structured Findings to the Editor; the final gate runs once, on what is posted.

4. **Skipped when there is nothing to refine.** A zero-Finding review has no Findings to drop or reword, so the Editor stage is skipped. The added cost lands only on PRs that carry Findings.

5. **Fail-closed.** An Editor stage that times out, crashes, or emits an invalid payload fails the PR-tick through `log_failure` (ADR 0005). Same-SHA dedup re-runs it on the next polling cycle, the same failure-then-retry path the Review agent already uses. No review is posted from a failed editorial pass.

> **Amended 2026-06-16 (#148).** The Editor's summary reconciliation (Decision point 2) gains the first codified Tone rule, the **severity floor**: the reconciled summary cannot read weaker than the highest-severity Finding that survives the Editor's drops. The rule's wording lives in the prompt SSOT (`review-agent-editor.md` itself since ADR 0010's 2026-07-21 amendment, `review-agent-default.md` §"Tone has a severity floor" before it); the Editor applies it to the surviving set, the only set whose post-drop max severity is known at reconcile time.
>
> The floor is one-directional because undersell is the documented failure while over-emphasis is already contained: the Editor's drop lever removes an over-labeled Finding, so a summary ceiling would guard a case the pipeline mostly handles, left to a future rule if dogfooding warrants one. It stays prompt-side because tone-to-severity coherence is a judgment: no deterministic check can tell an `important` Finding fairly weighted from one underplayed without reading the prose, the same false-positive risk that kept the seven #144 facets out of `voice.py` (ADR 0010 §4).
>
> `voice.py` is untouched, consistent with the ADR 0010 §4 boundary that #144 reaffirmed for the seven prior semantic facets.

> **Amended 2026-07-21 (#222).** The drop test branches on `type`. The Editor
> drops a Finding whose claim the code at HEAD does not support, which for a
> `intent` Finding (ADR 0002, ADR 0035) describes the expected state rather
> than a defect: the code is correct, it just is not what the change said it
> would be. Applied unchanged, the rule would delete every Finding the intent
> lens produces before it posts.
>
> So an `intent` Finding is verified against the other side of its comparison.
> The Editor re-reads the PR's own description, and the linked issue when the
> Finding cites one, and drops the Finding when the claim actually holds. It is
> still a verification against evidence, and the evidence is still re-gathered
> first-hand; only which artifact is read changes. Every other Finding type keeps
> the HEAD-code test unchanged.
>
> The Editor's fresh independent re-read (Decision point 1) is what makes this
> safe to branch. It is not being asked to trust the author's reading of the PR
> body, only to do its own.

> **Amended 2026-07-23 (#258).** The fail-closed model is reversed for a
> deterministic editor-output failure: rather than discard the review, the daemon
> posts the merged author draft with a bypass note. This adopts the "Degrade to
> the author's review when the editor fails" option this ADR originally rejected,
> on evidence that did not exist when it was written.
>
> The rejection weighed a robustness gain against "the voice gate runs in two
> places and the full author path stays alive as a second posting route," and
> took the simpler model. #258 supplied the missing cost of that simplicity: an
> editor miscount (17 decisions for a 16-finding draft) failed `apply_edits`'s
> coverage check and discarded the review whole, posting nothing. The miscount is
> deterministic, so the same draft loses its review every cycle. The trade the
> original rejection did not have was "lose robustness" versus "lose the review
> permanently on a class of editor error," which inverts the call.
>
> Both original objections are answered. The author path already runs the gate
> fail-open (the zero-finding skip, Decision point 4), so the fallback adds no
> second gate site; it reuses that path. The second posting route is no longer
> silent: `append_editor_bypass_note` marks the summary "Editorial cleanup did not
> run on this review," so a degraded review is distinguishable from a clean one.
>
> The reversal is scoped to **deterministic** failures, the ones that recur every
> cycle: a non-covering, unparseable, schema-invalid, or fidelity-corrupt decision
> set. `edit-fidelity` is included though #220 kept it fail-closed, because the
> fallback posts the clean author bodies, never the editor's corrupted re-emission,
> so #220's concern (do not post malformed text) is satisfied by the discard, not
> violated. **Transient** failures stay fail-closed: an editor timeout or empty
> output is retried cleanly by the next polling cycle, so losing this cycle costs
> nothing, and those paths exit before `apply_edits` (ADR 0005 amended).
>
> Two limits. The strict coverage check stays exactly as it was: a partial,
> positional apply of a miscounted decision set would attach a kept body to the
> wrong finding's `path` and `line`, worse than posting nothing, so recovery is
> whole-draft-or-discard, never a subset. And the fallback summary is the author's
> un-reconciled lead, so the severity floor (#148 amendment) does not apply to it;
> the bypass note discloses that the review is un-edited, which is the honest
> signal a floor would otherwise enforce.

## Boundary

This ADR decides the editorial pass over content. It does not touch the severity/type taxonomy (ADR 0002), finding relocation (anchoring), the format layer (ADR 0010 §1 to §3), or the reply path. The voice gate's *scope* is unchanged by ADR 0010 §4; only its pipeline position moves.

## Consequences

- A second `claude -p` stage runs on every findings-bearing review, roughly doubling per-review model cost and latency on those PRs. Clean PRs are unaffected (the stage is skipped). This is the accepted price of the quality differentiator.
- `voice.py`'s call site moves out of the author's extraction step to a gate after the Editor. The schema validation stays with the author parse so the Editor receives structured Findings.
- The daemon gains one stage, one timeout, one failure surface, and one log line, each independently observable and testable, consistent with the staged architecture.
- Semantic content judgment now exists in the pipeline, made by an agent rather than a deterministic check. This does not reopen ADR 0010 §4: the deterministic gate still enforces only lexical and structural rules; the judgment lives in a separate agent stage, which is exactly where a judgment belongs.
- A trial pass over three real PRs validated the design before build: the Editor dropped every planted weak Finding and kept every real one (grounded in its own re-read, not pattern-matching), and rewriting deliberately broken-but-real Findings produced shorter, lead-first, voice-clean bodies. The trial also surfaced the reserialization-fidelity risk that the by-reference output contract (point 2) and the fidelity check (point 3) address.
