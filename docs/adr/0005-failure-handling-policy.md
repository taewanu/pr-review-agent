# ADR 0005: Failure-handling policy for PR-ticks

Date: 2026-05-20
Status: Accepted

## Context

A PR-tick runs an LLM (the review agent) and a deterministic pipeline (extract →
validate → post). Failures arrive in two flavors:

- **System failures:** the review agent produced no parseable structured
  output, or auth/network broke. Cannot be fixed by retrying the same tick.
- **Per-finding failures:** the payload parses fine but one item violates a
  schema constraint (forbidden severity×type combo) or a positioning
  constraint (`line` outside diff).

The PRD demands "fail loud on errors" but also names a specific case (`line`
outside diff) where the daemon must convert the inline into a summary fallback
rather than abort. The two pulls need to be reconciled.

## Decision

A PR-tick stops on system failures and degrades on per-finding failures.

- **System failures:** log an actionable error to stderr, exit non-zero, post
  nothing. Same-SHA dedup makes the next tick retry naturally.
- **Per-finding failures:** drop or relocate the affected finding and
  continue. Always surface the action in the posted summary so degradation is
  visible, not silent.

## Consequences

| Failure | Category | Action |
| --- | --- | --- |
| Review agent stdout empty | system | exit non-zero, no post |
| Trailing JSON fence missing | system | exit non-zero, no post |
| JSON parse error | system | exit non-zero, no post |
| Schema invalid (required missing, enum off) | system | exit non-zero, no post |
| Style violation: `style-violation` (em dash, forbidden opener, or task-scoped ref in a summary, comment body, or reply body — shared rules in `voice.py` per ADR 0010) | system | exit non-zero, no post |
| Forbidden combo (`polish + important`) | per-finding | drop, note in summary, post remaining |
| `line` outside diff | per-finding | move body to summary's `## Findings outside the diff` section, post remaining |

- The summary's `## Findings outside the diff` section is the canonical relocation
  surface for findings the daemon could not anchor inline. Pattern mirrors
  Anthropic Code Review's relocation of out-of-diff findings.

> **Amended 2026-06-10 (#115).** The relocation heading is renamed `## Additional findings` → `## Findings outside the diff`. "Additional" named sequence ("more findings"); the defining property is the cause: the finding's line is not in the diff's changed hunks (`anchor_findings.py` could not anchor it), so it is relocated to the body. The new name states that cause, echoing the established term (CodeRabbit's "Outside diff range", GitHub's "outside the diff").
- The set of "per-finding failures" is closed at two entries. Any future
  category discovered defaults to **system / loud** unless this ADR is revisited.
- Until state tracking lands, system-failed ticks retry by hand. After it
  lands, retries happen automatically on the next poll cycle if the HEAD SHA
  has not changed.
