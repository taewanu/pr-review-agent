# ADR 0035: An intent lens reads the PR's own claims

Date: 2026-07-21
Status: Accepted

## Context

Every review lens reads only the code, so a change that contradicts what it
promised is invisible to all of them. The five lens agents
(`.claude/agents/review-agent-{default,correctness,perf,security,tests}.md`)
take the same two inputs: a PR URL and a line-numbered diff. None is given the
PR's description, the originating issue, or any acceptance criteria. A lens
cannot flag a contradiction it was never shown both sides of.

This is visible from the files, not from a measurement, which is why it is
actionable while the recall questions closed alongside it (#185, #188, #218,
#232) were not.

**Demonstrated on PR #230, 2026-07-21.** Matt Pocock's two-axis `code-review`
skill, run against that PR, caught two defects the daemon reached neither of.
The PR said `Closes #226` while skipping the measurement #226 asked for, and it
claimed all seven documented voice facets survived the move when two had been
dropped rather than moved. Neither is a code defect. Both are the change failing
to be what it said it was.

### What transfers from Pocock's skill is the axis, not the input

Pocock's skill is an insider tool. Whoever runs it knows which issue the PR is
for and points the skill at the repo's own standards documents. This daemon is
an outsider: it arrives at a PR URL holding only what the PR itself carries, on
repos whose conventions it does not know and should not assume.

Ranking candidate inputs by how reliably an outsider gets them:

| input | availability |
| --- | --- |
| PR title and body | always fetchable, sometimes empty |
| the issue behind `Closes #N` | only when the author wrote the reference and the issue is readable |
| a PRD, ADRs, `CLAUDE.md` | present in the clone, but meaningful only where the repo writes things down |

Only the first is guaranteed. So the honest question for a general tool is not
"does this conform to the spec" but "does this change do what it says it does".
Self-consistency needs no repo knowledge, works on a stranger's PR, and is
exactly what caught both of PR #230's defects.

That framing also answers the objection a fixed spec lens cannot: a PR with an
empty body supplies nothing to contradict, so the lens finds nothing and costs
nothing, rather than inventing a spec to measure against.

## Decision

**1. A sixth lens, `intent`, reads the change's stated intent against its diff.**
It joins the existing lens arrays in `daemon/review-pr.sh` and is governed by
`REVIEW_LENSES` like any other (ADR 0034), so an operator can drop it.

**2. Its inputs are a ladder, and it degrades to silence.** The PR title and
body always. The body of the issue behind `Closes #N` when GitHub reports a
closing reference and the fetch succeeds within its timeout. Nothing else. When
the fetch fails or no reference exists, the lens runs on the PR body alone and is
told so explicitly, so a missing rung reads as less evidence rather than as
license to guess. When neither rung yields substantive text, the lens is skipped
before dispatch and costs nothing.

The issue fetch is timeout-bounded like every other per-PR network call. An
unbounded fetch is how the daemon froze indefinitely once already (#121).

**3. Findings carry the new `intent` type** (ADR 0002, amended here). The
remedy is certain and the culprit is not: the author may fix the code to match
the description or fix the description to match the code, and the lens cannot
tell which was intended.

**4. The editorial pass verifies an `intent` finding against the PR's claims,
not against the code at HEAD.** `review-agent-editor.md` drops a finding when
"the code at HEAD does not support it", which for an `intent` finding is the
expected state: the code is fine, it is just not what was promised. Left
unchanged, that rule would delete every finding this lens produces before it
posts. The rule now branches on type (ADR 0016, amended).

## Considered and rejected

- **Reading `CLAUDE.md`, ADRs, and other repo documents as the spec.** Closest to
  Pocock's skill and strongest on this repo, weakest as a general tool. On a
  stranger's repo it either finds no documents or reads someone else's
  conventions as requirements this PR agreed to.
- **Naming it the spec lens, with a `spec` type.** Both mislead where no spec
  exists, which is most repos. A label sends the author looking for a document
  that is not there.
- **Framing it as scope discipline, with a `scope` type.** Narrower and clearer,
  and it would catch a real class nothing catches today ("fixes a typo" against a
  diff that rewrites auth). But `scope` names the size and boundary of a change,
  so it misdescribes the demonstrated cases: a false statement in the body is not
  a scope problem. Scope discipline is a subset of self-consistency and is
  reachable from this lens without its own type.
- **Deciding the whole lens set per PR from its available inputs**, the larger
  idea in #222 that also absorbs #219. Still the right direction. The skip-when-no-intent
  branch here is its first instance, and generalizing it needs the routing work
  to land first.
- **Defaulting the `perf`, `security`, and `tests` lenses off**, which #222 asked
  for alongside this lens. The shipped default stays at all six. The case for
  turning them off is that nothing in this repo's eval corpus is a perf,
  security, or test-quality defect, which is a fact about this corpus rather than
  about the forks that would inherit the change, some of which exist because
  security review is the point. An operator who shares the doubt narrows
  `REVIEW_LENSES` in their own `.env` at no cost to anyone else (ADR 0034). That
  is the reversible half of the decision, and the half this repo took.

  > **Amended 2026-07-23 (#249).** Shipped: the unset default drops to `default
  > correctness intent`, with `perf`, `security`, and `tests` off. The half above
  > is inverted, on the standing evidence that no perf, security, or test-quality
  > finding has ever survived review in this repo. The burden of proof sits on
  > keeping a default-on lens, not on removing it, so the lean default ships and a
  > fork where a domain is the point re-enables it in `.env` at no cost to anyone
  > else. The six stay selectable; only the unset default changed, set in the
  > daemon and `templates/.env.example` together against the config-drift trap.
- **Renaming the lens set's axes**, also asked for in #222: `default` is a sweep
  and `correctness` is a second deeper read of the same ground, so presenting the
  six as peers hides that dropping the sweep drops whole defect classes. Left
  open. It reaches every lens rather than this one, and folding it in here would
  make a naming change ride along with a new axis.

## Consequences

- A zero-finding `intent` result now means something. The lens either ran and
  found no contradiction, or was skipped for want of an input, and the daemon log
  distinguishes them.
- The taxonomy grows a fourth `type`, touching `daemon/extract_json.py`,
  `daemon/create-review.sh`'s emoji map, `daemon/anchor_findings.py`'s forbidden
  combinations, and the other lens prompts, which are told not to emit it.
- A `intent` finding anchors like any other. It cites the file the broken
  claim is about, so it renders inline when that file is in the diff and falls
  through to the "Findings outside the diff" section when it is not (ADR 0018).
  No new posting surface.
- The editor's drop rule is no longer uniform across types, which is a cost. It
  buys the only thing that lets an `intent` finding survive to posting.
- Whether this raises recall is not claimed and is not measurable at this
  project's PR volume, which is why #185, #188, #218 and #232 are closed. What is
  claimed is narrower and checkable from the files: a defect class that no lens
  could reach now has a lens that reads both sides of it.
