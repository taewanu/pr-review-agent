# ADR 0025: Per-finding schema validation

Date: 2026-07-06
Status: Accepted. Extends ADR 0024's "one bad component must not sink everyone" principle one level deeper: from the lens to the finding.

## Context

`extract_json.parse_payload` validated a lens's entire payload as one pydantic model: `ReviewPayload.model_validate(data)` built `comments` as `list[Finding]` in a single call, so one finding with a bad field failed validation for the whole list, discarding every other finding in that same payload, however valid.

ADR 0024 already fixed the analogous bug one level up: `merge_findings.merge()` no longer lets one lens's totally-unparseable payload sink the other lenses. This ADR's bug survives that fix, because it happens *inside* one lens's own otherwise-successful parse: the lens's payload has a valid `summary` and a `comments` list pydantic can iterate, but one entry in that list is malformed.

Confirmed live and load-bearing, not just theoretical: re-running the multi-lens pipeline (ADR 0023) against sounds-abroad#165, the PR whose single-generator `No findings` motivated this whole review-quality epic (project memory `project_review_quality_confidence`, epic #185), reproduced the exact original diff scope, dispatched all 5 lenses, and merged their raw output through the real `merge_findings.py`:

- The `correctness` lens independently found the actual bug (confidence 68): a `currentCountryCode` guard trusts that it always names the country `currentTrackRank` indexes into, but `audio-store.ts`'s resume path can update the track without updating the country, so resuming a cross-charting song from a different country's row scrolls an unrelated row into view in the original country's list.
- The `tests` lens independently flagged the same location (confidence 90) from a different angle: the guard's match branch, the one production always takes, has zero test coverage, so a regression collapsing the guard would pass every existing test.
- The `tests` payload also contained an unrelated second finding with `severity: "minor"`, not a valid enum value. Before this ADR's fix, that one bad entry failed the whole payload's `ReviewPayload.model_validate`, discarding the valid 90-confidence finding on the real bug along with it. `correctness`'s 68-confidence finding, with no surviving corroboration to lift it (the bodies did not cluster as the same defect under `merge_findings`'s similarity threshold), was gated out on its own. **Net result before this fix: zero findings survived, reproducing the original miss even with two independent lenses having found the bug.**
- After this fix: the `tests` finding survives per-finding validation (only its sibling entry is dropped and logged), clears the confidence gate on its own, and is the one finding in the final merged payload. The multi-lens design's core hypothesis, that redundant independent generation catches what a single generator's self-censorship misses, held on the exact case that motivated it once this narrower bug stopped discarding the evidence.

## Decision

`parse_payload` (`daemon/extract_json.py`) validates `summary` and structural shape (`comments` must be a list) at the payload level, then validates each entry in `comments` independently via its own `Finding.model_validate(item)` call. A finding that fails is dropped and logged to stderr (`finding-skip: comments[N] failed validation: <reason>`), matching the naming convention of ADR 0024's `merge-skip` line; every other finding in the same payload is unaffected. A payload where every finding happens to be invalid degrades to an empty `comments` list, the same outcome as a lens that genuinely found nothing; this is not escalated to a distinct failure category; the point of this ADR is that one bad entry should not read as a lens-wide failure, and an all-bad payload is not a new failure mode this ADR needs to invent one for.

Payload-level fields still fail the whole payload: a non-string `summary`, or a `comments` field that is not a list at all, have no per-item structure to salvage the way one bad entry in an otherwise-valid list does.

This lives in `extract_json.py`, not `merge_findings.py`, so both the single-agent path (`extract()`, used by the pre-ADR-0023 single-generator flow and the editor's own parse) and the multi-lens merge path (`merge_findings.py` calls `parse_payload` per lens) share the same fix with no duplicated logic.

## Boundary

This does not change the confidence gate, the cap, or the merge/cluster logic (ADR 0023); it only changes how many findings survive to reach them from a single lens's payload. It does not add a distinct category for "every finding in this payload was invalid": that is treated as equivalent to a lens finding nothing, not a new system failure.

## Consequences

- A lens's one malformed finding no longer costs the operator every other finding the same `claude -p` call produced, whether or not that lens's payload is being merged with others.
- The dogfood re-run of sounds-abroad#165 through the current pipeline (5 lenses, merge, confidence gate) now surfaces a finding at the exact location the original single-generator review missed, which is the first concrete evidence since epic #185 opened that the recall/precision split it introduced is actually working end to end, not just at the generation stage.
- `finding-skip` joins `merge-skip` (ADR 0024) as a stderr line the operator can grep the `.daemon.log` for to see which specific lens outputs are drifting from the schema, without those drifts silently costing recall.
