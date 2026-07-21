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
leg any way other than five subagent-dispatching lenses, so the cost question
#209 asks — what should the default be — could not be experimented with on
`main` at all.

Measured on real bug fixtures with Opus, both arms with the confidence gate
opened (threshold 0) so each finding's score is observed rather than pre-filtered
— an earlier comparison at gate 80 read `single-agent` as 0/3, but that was the
gate discarding real bugs it scored 60-78, not the mode missing them:

| config | sounds-abroad#217 | #145 | matched-bug confidence | tokens/review |
|---|---|---|---|---|
| subagent, 5 lenses | 3/3 | 2/3 | 65-87 | 1.7M-2.3M |
| single-agent, 5 lenses | 3/3 | 3/3 | 60-85 | 1.0M-1.7M |

**`single-agent` matches recall at 27-39% fewer tokens.** Over the two hard
fixtures it caught 6/6 to `subagent`'s 5/6, at effectively the same confidence,
and every one of the 55 findings beside the planted bug was judged non-noise. The
subagent layer — a parent `claude -p` whose slash command only dispatches one
subagent and forwards its stdout — is not buying recall or precision here; what
it costs is a second harness load per lens.

Two caveats the numbers carry. The gate at its shipped 80 drops real bugs from
*both* modes (scores reach down to 60), so it is miscalibrated independently of
this decision. And whether the subagent layer is worthless or merely redundant
with process isolation is not settled: the fair test is `fanout` (one parent,
five subagents — the subagent layer without the doubled harness), which is not
measured yet (#217). This decision rests only on the measured `single-agent`
vs `subagent` gap, not on a claim that subagents are valueless.

Judged noise on clean PRs was 0.067 findings/PR for five lenses and 0.000 for
one, over 30 runs, so the lens count does not trade precision. Token cost is not
a per-config constant either: the saving showed on smaller diffs and shrank on
the larger one, since the harness is fixed overhead and the diff-proportional
part dominates a large PR.

## Decision

Two independent dials.

1. **`REVIEW_MODE`** picks how each lens runs: `subagent` dispatches a subagent
   per lens; `single-agent` runs the lens in the process that already isolates
   it, with the agent's own body appended to the base system prompt and slash
   commands disabled. Resolved through `resolve_tunable`, so `.env` works. The
   code default when the key is absent is `subagent`, leaving an existing `.env`
   unchanged; the shipped template recommends `single-agent` on the measurement
   above.

2. **`REVIEW_LENSES`** picks how many lenses run, as a space-separated label
   list. Unset keeps all five. The merge and confidence gate are already
   lens-count-agnostic (ADR 0023 Decision 3).

They are orthogonal: `REVIEW_MODE=single-agent` with `REVIEW_LENSES` unset is
five single-agent lenses, not one. Nothing else is coupled to either dial — in
particular the mode does not touch `CONFIDENCE_THRESHOLD`. An earlier draft
lowered it to 40 under the cheaper mode, which both overrode the operator's own
`.env` value and made the two modes incomparable, since the cheaper one was then
scored behind a looser gate than the config it was measured against.

## Considered and rejected for now

- **A tools-free mode.** Dropping the lenses' tools is where a hard saving would
  come from, but it needs a pre-built context pack to review against, and that
  pack changed neither recall nor tokens in two separate A/B runs while the
  lenses kept their tools (ADR 0029 stays `Proposed`). A tools-free lens without
  a pack is unmeasured, so it does not ship.
- **A size-based auto-router.** Diff size is a weak proxy for difficulty, the
  router could not reach the subagent path at all, and no measurement covers it.
  Difficulty routing is tracked in #219.

## Consequences

- The shipped template recommends `single-agent`, so a fresh install and anyone
  copying the template get the cheaper mode; an existing `.env` without the key
  keeps `subagent` until edited.
- `single-agent` halves the process count per lens at matched recall, so the
  subagent layer is off the review's critical path. Whether it has value at all
  waits on the `fanout` measurement (#217); this ADR only claims the measured
  gap.
- The confidence gate at 80 drops real bugs both modes score in the 60-79 band.
  That is a miscalibration independent of the mode, not fixed here; the
  confidence-recording added to the eval harness for this decision is the input a
  recalibration would use.
- Whether five defect-category lenses are the right axis at all is open (#222),
  and whether the lens prompt's voice rules cost verification attention is open
  (#226).
- Whether five defect-category lenses are the right axis at all is open (#222):
  none of them reads the originating issue, so a diff that contradicts its spec
  is invisible to every one.
