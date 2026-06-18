# ADR 0012: Version-first release naming

Date: 2026-06-10
Status: Accepted (amended 2026-06-18)

## Context

Release markers carry the internal planning name while the consumer-facing name has no git surface. Tags are `phase-0`..`phase-6` and the open milestone is `phase-7`, yet the version every consumer sees (`0.2.1` in `pyproject.toml`, rendered in the preview banner on every posted review) exists nowhere in git: a banner reader who looks for a `v0.2.1` tag finds only `phase-6`. The roadmap issue (#88) bridges the two with a phase-to-version table and a V`A`.`B` → `0.A.B` naming convention, so one release currently answers to three names (phase-7, V2.2, 0.2.2).

The Phase layer also no longer carries information of its own. CONTEXT.md defined a Phase as a themed band where multiple Phases compose into a Version, but since `phase-4` every phase has mapped 1:1 to a release (phase-4 = V1, phase-5 = V2, phase-6 = V2.1), and themed grouping inside a release is already served by the roadmap's "arc" term. The project's own posting rules treat phase numbers as rot-prone task-scoped names (`daemon/voice.py` rejects `Phase N` in posted output), while a git tag is the most permanent public surface the project has.

## Considered options

- **Keep phase-first, document the mapping better**: leaves the broken banner-to-tag trace and three names per release, and the mapping table grows with every release.
- **Dual naming (tag both `v0.2.2` and `phase-7` on the release commit)**: alias tags keep the degenerate layer alive and double the surfaces to keep in sync.
- **Version-first (chosen)**: the semver is the release's one name; Phase becomes a historical term.

## Decision

From the next release on, the semver is the release's only name.

- The release marker is an annotated git tag `v0.A.B` (v-prefix, the ecosystem convention) with the established `## Added` / `## Fixed` / `## Trip-ups` message format. The `pyproject.toml` bump lands in the tagged commit, so the tag's tree carries its own version (the `phase-6` tree still says `0.0.0` because the bump trailed the tag).
- The planning milestone for the next release is named after the tag it will become (`v0.2.2`). The `phase-7` milestone is renamed, not recreated; its issues stay.
- The V`A`.`B` display names (V1, V2, V2.1) and the V`A`.`B` → `0.A.B` mapping convention are retired. Existing docs keep them only as history.
- Phase becomes a historical term: the `phase-0`..`phase-6` tags stay untouched, and the roadmap's release table remains the bridge from phase tags to semver. No new tag or milestone uses the name.

## Consequences

- The banner version, `pyproject.toml`, the milestone, and the tag all say the same thing; tracing any one to the others needs no table.
- Git history carries two naming eras: `git tag` lists `phase-*` then `v*`. The roadmap's release table is the durable map for the first era.
- CONTEXT.md's Version and Phase glossary entries are rewritten to match: Version gains the semver axis, Phase is marked historical.
- If GitHub Releases are adopted later, `v0.A.B` tags sort and resolve naturally with default tooling (`gh release create v0.2.2 --generate-notes`).

## Amendment (2026-06-18): GitHub Releases hold the detailed record

GitHub Releases were adopted at `v0.2.2`, so the hedged "if adopted later" above is now settled, and two practices drifted from the original Decision. This amendment records the corrected convention.

- The **GitHub Release body is the deep, per-issue record** of a shipped version: what broke, why, the fix, the ADR, the squash SHA. The roadmap (#88) keeps only a lean index per version (a theme line, the issue list, a link to the Release), so the two surfaces differ by depth rather than duplicating.
- The Release body is organized `## Added` / `## Fixed` / `## Changed` / `## Trip-ups`. `Changed` extends the original three-section format for refactors and hygiene that add no capability and fix no bug.
- The tag stays **annotated** (`git tag -a`, as the Decision already requires). Cut the tag first, then `gh release create v0.A.B --verify-tag` consumes it; `gh release create` on a missing tag mints a lightweight ref, which is how `v0.2.2`'s tag regressed.
