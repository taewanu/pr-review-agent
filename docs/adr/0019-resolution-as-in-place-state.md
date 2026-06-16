# ADR 0019: Thread resolution recorded as in-place state, not threaded notes

Date: 2026-06-16
Status: Accepted. Supersedes ADR 0017's `_Fixed:_` note (safety layer 2); the rest of ADR 0017 stands.

## Context

Two resolution drivers each posted their own threaded note and their own per-tick summary review: reply-driven (#75) posts a `_Confirmed:_`/`_Withdrawn:_` ack, commit-driven (ADR 0017) posts a `_Fixed:_` note. When the Operator both fixes a Finding in a commit and replies "done", both drivers fire on the one thread, and the thread carries two acks for one fix plus two "conversation resolved" summary reviews. Live case: sounds-abroad #113, where a single nit thread got a `_Fixed:_` note and a `_Confirmed:_` ack seconds apart (#159).

The root cause is modeling resolution *status* as an append-only event. Each driver appends a note, so two drivers on one thread append two notes. ADR 0017 point 4 gave the `_Fixed:_` note a sentinel namespace disjoint from the Reply sentinel on the assumption that commit-driven resolution has "no reply"; the overlap case (fix and reply for the same Finding) fell outside its frame. Patching the overlap with another cross-driver sentinel is the path #153 already took once, and it scales badly: each new driver collision needs a new sentinel.

The domain model already says the right thing. CONTEXT.md defines thread resolution as one concept with *two drivers*, but the code implemented two independent resolvers, each judging, acking, summarizing, and resolving on its own. The mismatch between "one resolution, two drivers" and "two resolvers" is the defect; the double-ack is its symptom.

## Considered options

- **Coordinate the two drivers with a shared sentinel (rejected).** When commit-driven resolves a thread carrying a pending Operator reply, stamp that reply addressed so the reply path skips it. This is the band-aid #153 established: it treats the collision, not the model, and the next driver pairing needs the next sentinel. It also suppresses at detection, before the reply's Verdict is known, so it would swallow an Operator question or pushback that happens to sit on a commit-fixed thread.
- **Unify the two drivers into one stage (rejected).** Tempting, but the drivers detect genuinely different things: commit-driven judges a fact about the code at HEAD, reply-driven reads an Operator statement. Each is the sole resolver for a case the other cannot see (a fixed-but-unreplied Finding; a withdrawn-as-false-positive Finding with no code change). ADR 0017's reasons for a separate, parallelizable, per-thread commit-driven stage still hold. The drivers stay two; what must become one is the *output*.
- **Record resolution as in-place state on the Finding's own comment (chosen).** The reference model (CodeRabbit) edits the Finding's comment to append the close reason rather than posting a new threaded note. Resolution status is then a single mutable slot per Finding, not an event stream, so two signals converge on one line instead of racing two notes.

## Decision

Resolution status is recorded as a **Resolution stamp**: a one-line status edited in place into the resolved Finding's Inline comment, never a new threaded note.

1. **Single slot, idempotent.** Both drivers write the same stamp line on the same comment. Two signals on one thread converge; they cannot produce two competing notes. This dissolves the double-ack structurally rather than coordinating it away.

2. **Commit-driven resolves silently.** On a positive per-thread judgment the daemon writes the stamp (naming the resolving commit) and resolves the thread. It posts no threaded note and opens no summary review. This supersedes ADR 0017's note-then-resolve (Decision point 3).

3. **Reply-driven keeps its threaded ack.** An Operator reply is a human message, so the daemon answers it with a threaded Reply ack, batched under the per-tick Reply review for one notification (#38). On a resolving Verdict (`confirmed`/`withdrawn`) it also writes the stamp. The ack stays an event because a conversation is one; only the resolution *status* moves to state.

4. **Stamp and ack are orthogonal.** The stamp is gated on resolution and lives on the Finding comment (silent edit). The ack is gated on an Operator reply and lives as a threaded reply (notifies). They never duplicate: a commit-only fix writes a stamp and no ack; a reply writes an ack and, if resolving, the same single stamp. The #113 case becomes one stamp plus one ack.

## Safety

ADR 0017's safety layer 1 (the safe-biased per-thread judgment) is retained unchanged: commit-driven resolution still fires only on a positive "defect gone at HEAD" verdict and defaults to leave-open under uncertainty, so wrong auto-closes stay rare.

Layer 2 changes from a notifying note to a silent stamp, and dropping the notification is acceptable because it was redundant for the primary use case. ADR 0017 posted the `_Fixed:_` note partly so the Operator would be *notified* of a possibly-wrong auto-close. But the Operator reviews their own PR before merging (own-PR auto-submit, ADR 0008), and a branch-protected merge forces them past the resolved conversations at the gate. They see the resolved threads and their stamps there, so a separate per-resolution ping duplicates a review they already perform. The stamp preserves what ADR 0017 actually valued: a visible, commit-named, reversible trace (resolving collapses a thread, it does not delete it, so a wrong close is reopenable). Only the active notification is dropped, and only for the silent commit-driven path; a reply still earns its threaded ack because a human is waiting on it.

## Boundary

This ADR changes how a resolution is *recorded and surfaced*, not when one happens. Candidate selection, the per-thread judgment, the Verdict vocabulary, the reply-driven trigger conditions (#75), and `voice.py`'s rules are unchanged. The empty `COMMENTED` review observed on #113 is a separate implementation defect (an opened wrapper with nothing to post), not decided here.

## Consequences

- The double-ack (#159) cannot recur: resolution status has one slot per Finding, so no pairing of drivers can write two notes.
- The two "conversation resolved" summary reviews collapse to one: commit-driven opens no review at all, leaving only the reply path's Reply review (#38), and only when a reply landed.
- Commit-driven resolution becomes silent. The trade is fewer notifications for a trace the Operator reads at their own pre-merge review rather than on a per-resolution ping.
- `_Fixed:_` as a threaded note is retired; CONTEXT.md gains the **Resolution stamp** term, the thread-level twin of the Status comment.
- A future driver (a third resolution signal) inherits the single-slot stamp for free, with no new cross-driver sentinel.
