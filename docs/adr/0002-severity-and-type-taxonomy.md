# ADR 0002: Severity and type taxonomy for review findings

Date: 2026-05-19
Status: Accepted

## Context

The review agent emits findings that the PR author has to triage. The labels attached to each finding shape that triage. Surveyed precedents:

- **Anthropic Code Review** uses a 3-level severity (`important`, `nit`, `pre-existing`) and no separate type axis.
- **CodeRabbit** uses 3 comment types (`potential issue`, `refactor suggestion`, `nitpick`) crossed with 4 severities (`critical`, `major`, `minor`, `trivial`).
- A free-form body with no enums was also considered.

Options were evaluated against three criteria: speed of triage for the PR author, consistency under LLM generation, and clarity of the decision tree.

## Decision

Use two enum axes on every comment:

- **`severity`**: one of `"important"`, `"nit"`, `"pre_existing"` (snake_case enum equivalent of Anthropic's `pre-existing`).
- **`type`**: one of `"bug"`, `"refactor"`, `"polish"`.

Both fields are produced by the review agent in the structured output and translated by the daemon into a visible body prefix at posting time.

## Consequences

- Two questions are answered at a glance: must I fix this before merge (`severity`), and is it a correctness problem or a polish suggestion (`type`).
- The `pre_existing` severity invites the review agent to call out issues in nearby unchanged code, separated from issues this PR introduced. This framing reduces author defensiveness.
- The `bug | refactor | polish` types collapse security and performance into `bug` (they are correctness problems) and the body text mentions the specific dimension when relevant. Avoiding separate `security` and `performance` types keeps the matrix small and forces the review agent to articulate why a finding matters in the body.
- The full 3 × 3 matrix is treated as:

  | type / severity | `important` | `nit` | `pre_existing` |
  | --- | --- | --- | --- |
  | `bug` | typical | allowed (edge cases) | allowed |
  | `refactor` | rare (reserve for "leaving as-is causes near-term pain") | typical | allowed |
  | `polish` | **forbidden** | typical | low-signal (discouraged) |

  Forbidden combinations are blocked in the review agent's prompt; rare and low-signal cases are discouraged in the prompt but not blocked.
- A 4-level severity (CodeRabbit's `critical | major | minor | trivial`) was rejected: the boundary between `major` and `minor` is fuzzy under LLM generation, and the PR author's decision is closer to a 3-way fork.
- A `nitpick` value that conflates type and severity (CodeRabbit's pattern) was rejected: keeping the axes orthogonal lets a refactor be `important` when it must, and a bug be `nit` when it is an edge case.
- Rendering is the daemon's responsibility. The review agent's output stays semantic and presentation-free: voice, tone, and nuance affect body wording only, not the structured fields.

## Rendering

The daemon translates each finding into two visible surfaces:

### Header: type first, severity second

Both dimensions render as parallel `_<emoji> <label>_` tokens (italic) separated by a pipe. Neither dimension is bolder than the other:

```
_🐛 bug_ | _🔴 important_
_🔧 refactor_ | _🟡 nit_
_✨ polish_ | _🟡 nit_
_🐛 bug_ | _🟣 pre_existing_
```

Italic keeps the header visibly lighter than the bold body lead, so a reader's eye lands on the conclusion first. CodeRabbit uses the same italic + pipe pattern.

Order matters: type (*what kind*) first, severity (*how severe*) second. A reader scanning a long review can separate the two axes at a glance.

The emoji maps:

| dimension | value | emoji |
| --- | --- | --- |
| `type` | `bug` | 🐛 |
| `type` | `refactor` | 🔧 |
| `type` | `polish` | ✨ |
| `severity` | `important` | 🔴 |
| `severity` | `nit` | 🟡 |
| `severity` | `pre_existing` | 🟣 |

`pre_existing` is severity-only; the type axis still applies (a pre-existing bug renders as `_🐛 bug_ | _🟣 pre_existing_`).

### Body: bold lead, optional bullets

The first non-empty line is the actionable conclusion in **bold**. Optional 2–4 bullets follow with mechanism, evidence, and suggested fix. Short findings skip the bullets:

```
_🐛 bug_ | _🔴 important_

**Drop `session.token` from the warning log.**

- `[^/.]+` rejects dots; GitHub allows them
- Effect: `my.cool.repo` falls through to env-var error
- Fix: loosen to `[^/]+?` + strip a trailing `.git` after match
```

Bullets are 0 or 2–4, never one. A single bullet is just a sentence with extra weight. The bold first line enforces 두괄식 (lead with the point) structurally: the visible shape carries the rule, not only the prompt examples.

Post-hoc validation in `daemon/extract-json.py` catches forbidden openers (`This`, `The`, `Consider`, …) on both plain and bolded leads. The validator peels a leading `**` before the prefix scan, so word-level rules apply inside the bold too. The bold-lead shape itself is not enforced: a body that ships plain prose still passes the validator. Opener voice is the load-bearing rule.
