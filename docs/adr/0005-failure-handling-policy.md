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
| Cosmetic voice miss in a posted payload: `style-violation` (em dash, forbidden opener, task-scoped ref; shared rules in `voice.py` per ADR 0010) | degrade | warn to the daemon log, post anyway |
| Editor produced no usable output: timeout or empty (`edit-timeout`, `edit-empty`; ADR 0016) | system | exit non-zero, no post (transient, retried next cycle) |
| Editor decisions unusable: unparseable, schema-invalid, non-covering, or fidelity-corrupt (`edit-no-fence`, `edit-parse-error`, `edit-schema-invalid`, `edit-coverage`, `edit-fidelity`; ADR 0016 amended, #258) | degrade | post the author draft with a bypass note |
| Forbidden combo (`polish + important`) | per-finding | drop, note in summary, post remaining |
| `line` outside diff | per-finding | demote per ADR 0040: file-level comment when the diff touches the path, else `## Findings outside the diff`; post remaining |
| `quote` matches no new-side line (ADR 0018) | per-finding | demote per ADR 0040, post remaining |
| `quote` matches several new-side lines and the emitted `line` corroborates none (ADR 0018) | per-finding | demote per ADR 0040, post remaining |
| A file-level comment's own POST fails (ADR 0040) | per-finding | render it in `## Findings outside the diff` instead, post remaining |
| Any exit that leaves the checks row's run open: system failure, watchdog TERM, Ctrl-C (ADR 0039) | system | conclude the run `neutral`, then flip the status comment; exit code unchanged |

- The summary's `## Findings outside the diff` section is the canonical relocation
  surface for findings the daemon could not anchor inline. Pattern mirrors
  Anthropic Code Review's relocation of out-of-diff findings.

> **Amended 2026-06-10 (#115).** The relocation heading is renamed `## Additional findings` → `## Findings outside the diff`. "Additional" named sequence ("more findings"); the defining property is the cause: the finding's line is not in the diff's changed hunks (`anchor_findings.py` could not anchor it), so it is relocated to the body. The new name states that cause, echoing the established term (CodeRabbit's "Outside diff range", GitHub's "outside the diff").

> **Amended 2026-06-15 (#155, ADR 0018).** Content-anchoring adds two per-finding relocation triggers (rows above): a `quote` that matches no new-side line, and a `quote` that matches several with no corroboration from the emitted `line`. Both relocate to the same `## Findings outside the diff` surface rather than anchor inline on a guess. ADR 0018 owns the anchoring mechanism; this table owns the degradation policy it routes through.

> **Amended 2026-07-28 (#191, ADR 0040).** The three relocation rows above no longer land in the body directly. A finding whose path the PR's diff touches becomes a file-level comment, which is a real thread; only one on a file the PR never touched reaches `## Findings outside the diff`, where it is advisory by design. The body section stays the canonical surface for that remainder and for a file-level comment whose extra request failed.

- The set of "per-finding failures" is closed at the entries in the table above.
  Any future category discovered defaults to **system / loud** unless this ADR is
  revisited.
- Until state tracking lands, system-failed ticks retry by hand. After it
  lands, retries happen automatically on the next poll cycle if the HEAD SHA
  has not changed.

> **Amended 2026-06-23 (#180).** System failures still exit non-zero and post no
> review object. The narrow change: when the durable status comment (#60, ADR
> 0020) is already live, the tick edits it to a `⚠️ Review failed … will retry`
> head-line instead of leaving it frozen at `👀 Reviewing…`. "Post nothing"
> predates the status comment; once the comment is posted *before* the review, a
> persistent failure that never touches it reads as a hung review. The flip is
> best-effort like every status-comment edit, so a failed flip never changes the
> exit code, and the next successful tick reuses the same comment and overwrites
> failed → Reviewing → Reviewed. Pre-comment preflight failures (auth, `git
> remote`, identity) stay silent: there is no live comment to update, and the
> author cannot act on an operator-environment error.

> **Amended 2026-07-19 (#220).** The post-Editor voice gate splits (the two rows
> replacing the old single `style-violation` row). A reserialization-fidelity
> corruption fails the review (`edit-fidelity`, exit non-zero, no post; superseded
> by the #258 amendment below), but a cosmetic style miss no longer discards a
> review that found a real bug: it posts with a `voice-warning` forwarded to the
> daemon log instead (CodeRabbit treats tone as customizable, never a gate that
> drops findings). This adds a third action, "warn and post", to the
> system/per-finding pair above.

> **Amended 2026-07-23 (#258).** The `edit-*` rows split by transience, and the
> deterministic set now degrades rather than failing closed. An editor timeout or
> empty output stays fail-closed: it is transient, and the next polling cycle
> retries it cleanly, so losing this cycle costs nothing. An unusable decision set
> (unparseable, schema-invalid, non-covering, or fidelity-corrupt) is
> deterministic, so fail-closed loses the review every cycle; instead the daemon
> posts the merged author draft with a bypass note (ADR 0016 amended). This
> reverses the #220 line above for `edit-fidelity`: the fallback posts the clean
> author bodies, never the editor's corrupted re-emission, so #220's concern is
> satisfied by discarding the corruption, not by discarding the review. The strict
> coverage check is unchanged; recovery is the whole author draft or nothing, never
> a positional partial apply of the miscounted decisions.

> **Amended 2026-07-21 (#231).** A subscription quota exhausted mid-review gets
> its own `session-limit` category and a fourth action, **pause and resume**. The
> quota case is a system failure by every existing rule (no review posts, exit
> non-zero, status flips), but the existing actions all assume the next cycle can
> do better. It cannot: every lens hits the same wall until the quota resets, so
> retrying every cycle for hours re-flips each open PR's status comment and
> teaches the author to ignore it. The daemon now records the reset as a deadline
> and skips polling until it passes, and the status head-line names that time
> instead of promising a retry next cycle.
>
> The category is decided **only** by the quota sentinel appearing on every lens
> output, never by the count of failed lenses. That direction is the whole safety
> property: inferring a quota hit from "all five failed" would let a genuine
> pipeline defect suppress polling for hours and hide itself, which is the failure
> this ADR's loud-by-default rule exists to prevent. A sentinel-free all-lenses
> failure keeps `all-lenses-failed` and its loud, retry-next-cycle behaviour.

> **Amended 2026-07-27 (#308, [ADR 0039](./0039-review-state-on-the-checks-row.md)).**
> The review now opens a check run before it starts, and a failure that leaves it
> `in_progress` is worse than the frozen `👀 Reviewing…` comment the #180
> amendment addressed: a stale comment misinforms, while a stuck check run can
> hold a merge back on a PR whose review is long dead. So the failure path
> concludes the run, and does it before the status-comment flip, since both
> handlers may be racing the per-PR watchdog's escalation from TERM to KILL. It
> fires on every exit that left a run open, not only a non-zero one, because the
> property worth holding is that no run of the script leaves one behind.
> `neutral` is the conclusion: no verdict was reached, and a daemon-side failure
> must not gate a merge the author cannot unblock. `review-pr.sh` also traps TERM
> and INT, which otherwise kill the shell with the EXIT trap unrun. What a
> `SIGKILL` still leaves behind is in ADR 0039's consequences.
