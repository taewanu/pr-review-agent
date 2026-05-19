# ADR 0002 — Severity and type taxonomy for review findings

Date: 2026-05-19
Status: Accepted

## Context

The reviewer persona emits findings that the PR author has to triage. The labels attached to each finding shape that triage. Surveyed precedents:

- **Anthropic Code Review** uses a 3-level severity (`important`, `nit`, `pre-existing`) and no separate type axis.
- **CodeRabbit** uses 3 comment types (`potential issue`, `refactor suggestion`, `nitpick`) crossed with 4 severities (`critical`, `major`, `minor`, `trivial`).
- A free-form body with no enums was also considered.

Options were evaluated against three criteria: speed of triage for the PR author, consistency under LLM generation, and clarity of the decision tree.

## Decision

Use two enum axes on every comment:

- **`severity`**: one of `"important"`, `"nit"`, `"pre_existing"` (snake_case enum equivalent of Anthropic's `pre-existing`).
- **`type`**: one of `"bug"`, `"refactor"`, `"polish"`.

Both fields are produced by the persona in the structured output and translated by the daemon into a visible body prefix at posting time.

## Consequences

- Two questions are answered at a glance: must I fix this before merge (`severity`), and is it a correctness problem or a polish suggestion (`type`).
- The `pre_existing` severity invites the persona to call out issues in nearby unchanged code, separated from issues this PR introduced. This framing reduces author defensiveness.
- The `bug | refactor | polish` types collapse security and performance into `bug` — they are correctness problems — and the body text mentions the specific dimension when relevant. Avoiding separate `security` and `performance` types keeps the matrix small and forces the persona to articulate why a finding matters in the body.
- The full 3 × 3 matrix is treated as:

  | type / severity | `important` | `nit` | `pre_existing` |
  | --- | --- | --- | --- |
  | `bug` | typical | allowed (edge cases) | allowed |
  | `refactor` | rare (reserve for "leaving as-is causes near-term pain") | typical | allowed |
  | `polish` | **forbidden** | typical | low-signal (discouraged) |

  Forbidden combinations are blocked in the persona prompt; rare and low-signal cases are discouraged in the prompt but not blocked.
- A 4-level severity (CodeRabbit's `critical | major | minor | trivial`) was rejected: the boundary between `major` and `minor` is fuzzy under LLM generation, and the PR author's decision is closer to a 3-way fork.
- A `nitpick` value that conflates type and severity (CodeRabbit's pattern) was rejected: keeping the axes orthogonal lets a refactor be `important` when it must, and a bug be `nit` when it is an edge case.
- Rendering is the daemon's responsibility: `severity` translates to an emoji prefix and `type` to a bolded label in the posted comment body. The persona output stays semantic and presentation-free.
