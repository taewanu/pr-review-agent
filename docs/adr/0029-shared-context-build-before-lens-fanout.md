# ADR 0029: Shared context-build before the lens fan-out

Date: 2026-07-10
Status: Proposed

## Context

The five-lens review leg (ADR 0023) spends most of its cost re-investigating the
same repository five times. Each lens is a full agentic `claude -p` call sharing
one scratch clone, and each independently runs the generate-verify-score method
(ADR 0022): read the diff, then `Read`/`Grep` the changed files, their callers,
and the surrounding code to construct a trigger scenario for every candidate. That
investigation is the verify step's whole substance, and it runs five times on the
frontier model with near-identical inputs, plus a sixth time in the editor
(ADR 0016), which also re-reads the PR at HEAD.

CodeRabbit's published architecture is the reference for where that cost belongs.
It builds a fresh structural graph of the repo per review (Tree-sitter ASTs,
dependency and call relationships, not pre-indexed RAG), assembles 10-15 data
points around the diff, and holds roughly a 1:1 code-to-context ratio, with 80-90%
of review tokens going to context enrichment rather than the final review pass
(CodeRabbit, "Context Engineering: Level up your AI Code Reviews" and "Code
context: the evidence behind trustworthy AI code review," verified 2026-07-10).
The context is built once, cheaply, and the reasoning pass consumes it.

Anthropic's own code-review tool keeps the opposite split: it runs specialized
agents in parallel, each analyzing the diff and the surrounding code itself, then
a verification pass tries to disprove each finding before it ships (Anthropic /
The New Stack / InfoQ coverage, verified 2026-07-10). Anthropic does not hoist the
investigation out of the per-agent loop; CodeRabbit does. This project already
runs Anthropic's parallel-lens shape (ADR 0023). The unclaimed lever is
CodeRabbit's: build the shared investigation once instead of five times.

The recall lever this project bought in ADR 0022 and 0023 is reasoning diversity,
not investigation diversity. Recall comes from five independent reads reaching
different candidates, verified and gated. Nothing about that requires each read to
re-walk the repository from scratch. Investigation and reasoning are separable: the
former can be shared, the latter must stay independent to keep the recall.

## Considered options

- **Collapse the five lenses into one rich-context reasoning pass (deferred to a
  later, measured step).** This is CodeRabbit's literal shape (one frontier pass
  over assembled context) and the larger cost cut. It also drops the reasoning
  diversity ADR 0023 chose for recall, and this project has no measured recall
  baseline yet to bound that trade. Deferred until the harness (#209 A1) produces
  the numbers to justify it; taking it blind would regress recall on the exact
  class ADR 0022 was written to catch.
- **Merge the five lenses into two or three domain lenses.** A middle cut on
  reasoning passes. Same unmeasured recall risk as the full collapse, smaller
  payoff, and it muddies ADR 0023's clean one-lens-per-concern mapping. Rejected
  for this cut for the same measure-first reason.
- **Build the shared context as a full Tree-sitter code graph.** Faithful to
  CodeRabbit, and the most precise caller/callee retrieval. It adds a Tree-sitter
  toolchain dependency to a bash-plus-Python daemon that runs on stock macOS
  (ADR 0013's runtime constraint), for precision the grep approximation below
  mostly recovers. Deferred as a later depth lever, not this cut's scope.
- **Deterministic shared context-build, five lenses unchanged in count (chosen).**
  Hoist the investigation into one pre-lens stage that gathers the changed
  symbols' references (their callers) and related tests deterministically (git
  plus grep, no model call), and hand every lens the result. The lenses keep their
  investigation tools as a fallback, so a gap in the pack degrades to today's
  behavior rather than blinding a lens. Recall cannot regress by construction: no
  reasoning read is removed and no information is taken away. Only the cost of
  redundant investigation moves.

## Decision

Add a shared context-build stage before the lens fan-out, keeping the five lenses
and everything downstream unchanged.

1. **`daemon/build_context.py` (new) builds a review pack once, before the lens
   loop, from the scratch clone at HEAD and the diff.** It is deterministic: no
   `claude -p` call, so it adds no frontier or cheap-model cost. It assembles, into
   one text file (`.pr-review-context.txt` in the scratch dir):
   - The changed symbols: the function and class definitions the diff's added
     lines introduce or edit. Bare assignments are excluded on purpose; a changed
     constant's name floods the reference grep with unrelated same-named matches
     for little caller-tracing value, so the extraction is biased to definitions.
   - For each changed symbol, its references elsewhere in the repo, found by grep,
     as `path:line` regions with a few lines of surrounding context. A grep on the
     symbol name yields undirected references (mostly the changed code's callers),
     not a directed callee map, so this is a caller-side approximation of
     CodeRabbit's AST call graph, not the full graph.
   - Related tests: test files that reference a changed file's basename or a
     changed symbol.
   - A short project-context header: the ADR index (titles only), so a lens's
     ADR-violation check does not re-read the tree to find them. `CLAUDE.md` is
     already loaded into every agent's context by the harness, so it is not
     duplicated into the pack.

   The changed files' own full content is not inlined: they already sit in the
   scratch working tree the lens can `Read` directly, so inlining would duplicate
   bytes the lens already has cheap access to. The pack's value is the retrieval a
   lens would otherwise grep for, not the files it can already open.

2. **Every lens and the editor receive `--context <basename>`.** `review-pr.sh`
   passes it alongside `--diff`, by bare basename (same `$TMPDIR`-with-a-space
   guard as the diff, ADR uses the basename so slash-command arg splitting is
   safe). The build runs once; all five lenses and the editor read the same file.

3. **Each `review-agent-*.md` and `review-agent-editor.md` gains a context-pack
   step in its method.** The instruction: a context pack has already gathered the
   changed symbols' references (their callers) and the related tests; read it
   first, and use `Read`/`Grep` only for what the pack does not cover. The tools stay in
   the allowlist (ADR 0023 Decision 17). The pack is a head start that removes the
   need to re-derive the shared map, not a replacement for a lens's own judgment
   about what else to open. This is a soft steer, not an enforced contract: a lens
   that distrusts the pack and re-greps still produces a correct review, just
   without the saving.

4. **The pack is additive and lossless, so recall is neutral by construction.**
   No reasoning pass is removed, no tool is taken away, and the pack only adds
   pre-gathered context a lens could have found itself. The worst case (a useless
   pack) is today's behavior plus one wasted deterministic build, not a blinded
   lens. This is the property that lets this cut ship before the recall baseline
   runs: it cannot move recall, only cost.

5. **The cost win is hypothesized, not asserted, and the #209 A1 harness gates the
   merge.** Keeping the investigation tools means the saving depends on lenses
   trusting the pack instead of re-investigating alongside it; a lens that reads
   the pack and greps anyway costs more, not less. The dry-run harness measures the
   actual before/after cost and recall on the fixture corpus. A merge requires a
   real cost reduction and no recall regression. If the measured saving is small
   because lenses double-investigate, that number is itself the evidence that would
   justify the deferred aggressive cut (remove the tools, collapse the passes),
   consciously trading recall with data in hand rather than blind.

## Boundary

This ADR adds one deterministic pre-lens stage and one input to each existing
agent. It does not change the lens count, the confidence rubric (ADR 0022), the
merge and gate (ADR 0023), the severity/type taxonomy (ADR 0002), the format layer
(ADR 0010), the editor's subtract-only contract (ADR 0016), or the reply path. It
does not add a Tree-sitter code graph, a cheap-model distill pass over the gathered
context, or any model call to the context-build: those are named as later depth and
cost levers, not built here. It does not collapse or merge lenses; that is the
deferred, measurement-gated step.

## Consequences

- The shared investigation is built once per PR instead of five-to-six times. The
  deterministic build itself is free of model cost; the win is the frontier tokens
  the lenses no longer spend re-deriving the same caller map.
- The saving is soft, bounded by how far the lenses trust the pack over their own
  tools. This is the deliberate price of the recall-neutral guarantee (Decision 4);
  a hard saving needs the tools removed, which is not recall-neutral and is
  deferred.
- The grep-based reference map is an approximation of CodeRabbit's AST call graph.
  It over-includes (a grep for a common symbol name catches unrelated matches) and
  under-includes (indirect calls, dynamic dispatch, renamed re-exports). A lens's
  retained tools cover the under-inclusion; the over-inclusion costs some pack
  bytes. AST precision is a later lever.
- Pack size grows the lens input. For a small PR the reference map is bounded; for
  a diff touching a widely-referenced symbol it can be large, and every lens pays
  that input five times. A cheap-model distill pass (deferred) is the lever if the
  raw pack proves too large in the harness.
- Adding a sixth lens later still costs one array entry in `review-pr.sh` plus the
  `--context` pass-through, both already lens-count-agnostic; the context-build is
  built once regardless of lens count, so a new lens adds zero context-build cost.
- The recall-neutral property makes this cut safe to ship on the harness's cost
  numbers alone. The harness's recall numbers still matter, but for the deferred
  aggressive cut, not for this one.
