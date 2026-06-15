# ADR 0018: Line-numbered diff input and content-anchored findings

Date: 2026-06-15
Status: Accepted

## Context

The review agent emits a finding's `line` by counting new-side lines off a raw `gh pr diff`, and it miscounts. On PR #154 a nit whose body was about the `MARKER` constant (file line 20) posted as an inline comment on line 32, off by 12. The miscount survives the pipeline: `extract_json.py` validates the integer, `anchor_findings.py` only checks the line falls inside some new-side hunk, and `create-review.sh` posts it as-is. For a new file the hunk is `@@ -0,0 +1,N @@`, so any line in `1..N` passes the range check.

Two harms follow. The inline comment sits on code unrelated to its content. And it breaks commit-driven resolution (#125, ADR 0017): a fix that touches the finding's *real* line falls outside the wrongly-anchored thread's increment range, so the thread is never a resolution candidate and stays open, blocking the merge gate the feature exists to clear.

The field names this "position drift" and converges on two complementary fixes: give the model line numbers to read instead of count, and verify the model's location against the quoted content (content wins over the emitted number). Neither alone is sufficient: a numbered diff still drifts by ±1, and content matching has nothing to match for findings that quote no single line.

## Considered options

- **Range-validation only (status quo, rejected).** A wrong-but-in-range line passes the only gate there is. This is the #155 bug.
- **Line-numbered diff alone (rejected).** Feeding the agent line numbers raises accuracy across every finding but stays unverifiable: a residual misread still posts on the wrong line with nothing to catch it.
- **Content-anchoring alone (rejected).** Robust for a precise single-line finding, but file-level, absence ("missing test"), and block findings quote no single line, so they would fall back to the unverified counted line or be relocated wholesale out of the inline view.
- **Both layers (chosen).** The numbered diff makes the emitted line trustworthy for the no-quote findings; content matching verifies and corrects the precise ones.

## Decision

Anchor findings from two layers over a safe-biased gate, mirroring ADR 0017's bias toward the harmless error.

1. **Line-numbered diff to the agent (layer 1).** The agent reads a diff whose new-side lines carry their new-file line number as a left prefix (`│` separator, U+2502, distinct from a `|` in code), and emits `line` by reading that number rather than counting. Only new-side lines are numbered: GitHub anchors inline comments to the new file and `create-review.sh` posts `side: "RIGHT"`, so old-side numbers are noise and deleted lines carry none. The raw `gh pr diff` is kept unchanged for the deterministic pipeline (`anchor_findings.py` range parse, commit-driven resolution); only the agent's copy is numbered.

2. **Content-anchored verification (layer 2).** A finding carries an optional `quote`: the exact source text of the flagged line, leading number and marker stripped. `anchor_findings.py` matches `quote` against the diff's new-side line text and, when matched, anchors to the matched line. Content wins over the emitted `line`.

3. **Confidence gate, safe-biased.** Never anchor inline on a guess.
   - `quote` matches exactly one new-side line: anchor there.
   - `quote` matches several: use the emitted `line` as the tie-breaker if it coincides with one match; otherwise relocate to `## Findings outside the diff` (ADR 0005).
   - `quote` matches none: the claimed text is not in the diff, so the finding is suspect. Relocate. The emitted line does not rescue it.
   - `quote` absent: the finding is region-level (file-level, an absence, or a block) and has no single line to verify. Anchor to the emitted `line` (now read off the leading number, not counted) under the existing range check.

4. **Whitespace.** Match on leading- and trailing-stripped text, internal whitespace preserved. Stripping leading absorbs the agent's most common indentation drift; preserving internal avoids collapsing two distinct lines into one ambiguous match.

5. **Schema stays optional.** `quote` is optional with graceful fallback, so a finding that omits it never fails the whole review. The agent prompt asks for `quote` on every single-line and block finding and to omit it only for genuinely line-less findings, making absence a deliberate "this is region-level" signal rather than noise.

## Boundary

This ADR decides how a finding's inline line is determined. It does not change the severity×type taxonomy (ADR 0002), voice rules (ADR 0010), or the Editor contract (ADR 0016): the Editor names a decision by index and never touches `path`, `line`, or `quote`, so `quote` rides through `apply_edits.py` by reference. It does not change the commit-driven resolution mechanism (ADR 0017); it restores the trustworthy inline anchoring that mechanism depends on. Inline comments stay RIGHT-side only.

## Consequences

- The per-finding relocation triggers in ADR 0005 gain two entries: `quote` ambiguous and `quote` no-match both relocate to `## Findings outside the diff`. ADR 0005's table is amended to reference this ADR.
- A finding relocated outside the diff has no inline thread, so commit-driven resolution (#125) cannot auto-resolve it. Acceptable: an unverifiable anchor could never auto-resolve correctly anyway, and the finding still ships in the review body.
- `anchor_findings.py` becomes the single home for diff-line indexing: hunk ranges (existing), per-line `(new_lineno, text)` for quote matching, and the line-numbered renderer all share one walk (`_iter_diff_lines`). No new pipeline stage.
- The two layers are belt-and-suspenders by design: layer 1 lifts base accuracy for every finding including the no-quote ones layer 2 cannot reach; layer 2 catches layer 1's residual drift on precise findings. Precedent: CodeRabbit and the broader LLM-review field treat content as authoritative over emitted line numbers.
