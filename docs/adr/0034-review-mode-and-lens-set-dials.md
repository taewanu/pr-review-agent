# ADR 0034: REVIEW_MODE and REVIEW_LENSES dials for the review leg

Date: 2026-07-20
Status: Accepted

## Context

The review leg runs five lenses (ADR 0023), each a `claude -p` whose slash
command does one thing: dispatch a subagent and forward its stdout unchanged
(`.claude/commands/review-pr.md`). The parent loads the Claude Code harness,
passes its child's output through, and exits. Each lens already runs in its own
process, so the subagent adds no isolation the process did not give, and the
harness is paid twice per lens.

That shape is also the only shape. Until now there was no way to run the review
leg any way other than five agentic lenses, so the cost question #209 asks —
what should the default be — could not be experimented with on `main` at all.

Measured on real bug fixtures with Opus, both arms behind the same confidence
gate of 80 (an earlier draft of this mode lowered the gate to 40 under `direct`,
which made every prior comparison read the cheap arm behind a looser bar than the
arm it was measured against; those numbers are void):

| config | sounds-abroad#217 | #145 | tokens/review |
|---|---|---|---|
| agentic, 5 lenses | 2/3 | 2/3 | 1.7M-2.1M |
| direct, 5 lenses | 0/3 | 0/3 | 1.0M-1.5M |

**`direct` costs 28-40% fewer tokens and catches nothing at the same gate.** It
does still surface the defect; it scores it below 80, so the gate drops it. That
is consistent with the shape: an agentic lens hands its investigation to a
subagent with a clean context, while `direct` interleaves the review instruction
and the investigation in one conversation, and the verify step (ADR 0022) is what
confidence is scored on.

Judged noise on clean PRs was 0.067 findings/PR for five lenses and 0.000 for
one, over 30 runs, so the lens count does not trade precision. Token cost is not
a per-config constant either: the saving showed on smaller diffs and shrank on
the larger one, since the harness is fixed overhead and the diff-proportional
part dominates a large PR.

## Decision

Two independent dials, neither changing the default behaviour.

1. **`REVIEW_MODE`** picks how each lens runs: `agentic` (default) dispatches a
   subagent per lens; `direct` runs the lens in the process that already
   isolates it, with the agent's own body appended to the base system prompt and
   slash commands disabled. Resolved through `resolve_tunable`, so `.env` works.

2. **`REVIEW_LENSES`** picks how many lenses run, as a space-separated label
   list. Unset keeps all five. The merge and confidence gate are already
   lens-count-agnostic (ADR 0023 Decision 3).

They are orthogonal: `REVIEW_MODE=direct` with `REVIEW_LENSES` unset is five
direct lenses, not one. Nothing else is coupled to either dial — in particular
the mode does not touch `CONFIDENCE_THRESHOLD`. An earlier draft lowered it to
40 under the cheaper mode, which both overrode the operator's own `.env` value
and made the two modes incomparable, since the cheaper one was then scored
behind a looser gate than the config it was measured against.

## Considered and rejected for now

- **A tools-free mode.** Dropping the lenses' tools is where a hard saving would
  come from, but it needs a pre-built context pack to review against, and that
  pack changed neither recall nor tokens in two separate A/B runs while the
  lenses kept their tools (ADR 0029 stays `Proposed`). A tools-free lens without
  a pack is unmeasured, so it does not ship.
- **A size-based auto-router.** Diff size is a weak proxy for difficulty, the
  router could not reach the agentic path at all, and no measurement covers it.
  Difficulty routing is tracked in #219.

## Consequences

- The default review is byte-for-byte what it was; every behaviour change is
  opt-in through a dial.
- `direct` halves the process count per lens and must not become the default on
  the measurement above: it is cheaper and empty-handed at the shipped gate. It
  stays as an experiment dial. What is still open is whether the findings it
  scores at 40-79 are real, which decides between lowering the gate for this mode
  and abandoning it for difficulty routing (#219). The precision judge added in
  #221 can answer that on the clean-PR corpus.
- Whether five defect-category lenses are the right axis at all is open (#222):
  none of them reads the originating issue, so a diff that contradicts its spec
  is invisible to every one.
