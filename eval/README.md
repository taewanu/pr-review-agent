# eval — recall/precision/cost harness fixtures (#209 A1)

Labeled PRs used to measure whether a review config still catches real bugs
(recall, `fixtures.jsonl`), how often it flags clean code (false positives,
`precision_fixtures.jsonl`), and at what token cost, before a cost-cutting change
ships. "Measure, do not guess" (the CodeRabbit eval-harness
discipline); one fixture passing once is not confidence, so the corpus grows to a
handful of independent cases.

Not run in CI: each case drives a real `claude -p` review and costs tokens. The
harness (A1c) is invoked manually against this manifest.

## fixtures.jsonl

One JSON object per line. Each pins a PR to the commit where the bug was live, so
the fixture stays reproducible even after the bug is fixed and the PR merged.

| field | meaning |
|---|---|
| `id` | stable slug, `<repo>-<pr>-<short-topic>` |
| `pr_url` | the PR |
| `at_sha` | commit where the bug is live (usually the finding's `original_commit_id`, i.e. the pre-fix commit). The harness reviews here via `review-pr.sh --dry-run --at-sha`. |
| `bug.path` | file the bug lives in |
| `bug.line` | approximate line, or `null`; the judge matches on path + summary, not exact line |
| `bug.summary` | one line describing the defect, precise enough for the judge to decide "did the review surface this" |
| `severity` | `bug` (only real correctness bugs belong here, not nits/style) |
| `source` | why this is a confirmed-real label (e.g. resolved thread, motivating incident) |

## How a fixture reproduces its bug

`at_sha` is the commit before the fix. `review-pr.sh --dry-run --at-sha <at_sha>`
diffs `base...<at_sha>` and checks out that commit, so the lenses see the buggy
code exactly as it was when the bug was live. Verified for the anchor case
(`sounds-abroad#165` at `b5de9d92`): the cross-country scroll guard is absent
there, present at the merge head.

**Invariant every fixture must satisfy:** `bug.path` appears in the
`base...at_sha` compare diff, where `base` is the PR's *actual* base branch (not
always `main`). Check with
`gh api "repos/OWNER/REPO/compare/BASE...AT_SHA" -H "Accept: application/vnd.github.diff"`.
A stacked PR whose bug lives in code introduced by its base branch fails this
(the file isn't in the PR's own diff) and is not a usable `--at-sha` fixture;
`sounds-abroad#185` was dropped for exactly this reason.

## Judging a run (A1c)

For each fixture, the harness runs the config, then an Opus judge compares the
config's findings against `bug.summary` at `bug.path`: did it surface the known
bug (recall)? Cost comes from the `.cost` sidecars in the preserved scratch. A
config that misses a fixture's bug regresses recall and must not ship (#185 is why
the lenses exist).

## precision_fixtures.jsonl

Merged PRs reviewed for noise rather than for a known bug. A precision fixture
carries no `bug` and no `at_sha`; the harness reviews it at the PR's current HEAD
via plain `review-pr.sh --dry-run` (`gh pr diff`, which works on a merged PR
where a `base...merge_sha` compare would be empty). Run it the same way, pointing
`--fixtures` at this file.

**The score counts findings a judge ruled noise, not findings posted.** Each
finding is graded against the diff it was made on: noise only if the diff
refutes it, it is factually wrong, the code already handles it, or it proposes
no actionable change. A true claim is not noise however minor, and a claim
resting on code outside the diff is not noise merely because the judge cannot
see that code. Uncertain calls resolve to "not noise", so the score is a lower
bound, kept comparable across configs by holding that bar fixed.

Counting every finding instead was the original design and it ran backwards: two
of these five fixtures drew findings that were all verifiable (a stale `poll.sh`
header, a README config list missing a key), so the most accurate reviewer
scored as the noisiest. Grading per finding also means a fixture no longer has
to be implausibly clean to be usable.

| field | meaning |
|---|---|
| `id` | stable slug, `<repo>-<pr>-<short-topic>` |
| `pr_url` | the merged PR, reviewed at HEAD (no `at_sha`) |
| `kind` | `precision` (documents intent; the harness keys off the absent `bug`) |
| `note` | why this PR is a clean negative |
| `source` | provenance |
