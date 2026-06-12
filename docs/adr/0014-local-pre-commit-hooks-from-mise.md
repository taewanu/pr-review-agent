# ADR 0014: Source pre-commit hooks from mise, not hook repos

Date: 2026-06-12
Status: Accepted

## Context

Every `.pre-commit-config.yaml` hook (`ruff`, `shellcheck`, `shfmt`, `actionlint`, `gitleaks`, `yamllint`) was pulled from an external pre-commit repo, each installed into its own isolated environment. But all six tools are **also** pinned and installed by `mise` (`mise.toml`), and the repo already mandates `mise exec -- pre-commit` in CLAUDE.md and CI. So each tool was sourced twice:

- **Double install** — mise installs all six; pre-commit re-installs all six from their hook repos.
- **Version drift** — two pins per tool, free to disagree. `shfmt` was `v3.13.1-1` (hook) vs `3` (mise); `ruff` was `v0.15.13` vs `0.15`.
- **Install-time network flake** — every hook environment install hits the network, so any can fail transiently. PR #91's `lint` failed on `HTTP Error 504` while `scop/pre-commit-shfmt` fetched the `shfmt` binary; it passed on re-run, no code at fault.

Replicating the upstream hooks surfaced a latent defect. pre-commit replaces a hook's manifest `args` with the config's `args`, so `shfmt`'s manifest `--write` was overridden away by `[-i, "2", -ci]`, leaving `shfmt -i 2 -ci` — which prints formatted output to stdout and always exits 0. The shfmt hook caught nothing, in both pre-commit and CI; three daemon files had drifted out of format under it.

## Considered options

- **Keep hook repos (status quo)**: the standard pre-commit pattern, self-pinning and usable without mise. But the standalone benefit is moot here since mise is already mandatory, leaving only the duplication and the flake.
- **Drop mise pins, let pre-commit own versions**: one source again, but inverts the repo's tool-manager choice and leaves CI/shell tooling on a different mechanism than the hooks.
- **`repo: local` / `language: system` hooks calling the mise binaries (chosen)**: one pin per tool (`mise.toml`), no per-hook download, no drift. Costs a deviation from the conventional self-pinning pattern.

## Decision

Convert all hooks to a single `repo: local` block of `language: system` hooks that invoke the mise-pinned binaries on `PATH`, run under `mise exec -- pre-commit`.

- Each hook's `entry`, `types`/`types_or`, `require_serial`, and `pass_filenames` mirror its upstream manifest at the previously pinned rev, so *what* runs is unchanged: `ruff … --force-exclude`, `shellcheck` with `[-x, --enable=SC2162]`, `actionlint` scoped to `^\.github/workflows/`, `gitleaks git --pre-commit --redact --staged --verbose` with `pass_filenames: false`, `yamllint` with the same inline config.
- `shfmt` gains `-d` so it exits non-zero on a formatting diff instead of passing silently. This restores the enforcement the args override had removed; the three drifted daemon files are reformatted in the same change so the now-live check is green.
- `default_language_version` is dropped: no hook builds a managed language environment anymore.

## Consequences

- One source of tool versions. Bumping a linter is a single `mise.toml` edit; the hook follows automatically.
- No per-hook network fetch at install or run, so the #91-class transient install failure cannot recur.
- The hooks now require the mise binaries on `PATH`. Under `mise exec -- pre-commit` (CLAUDE.md, CI) they resolve; a bare `git commit` from a shell without mise activated would not find them. This is the same `mise exec` assumption the repo already makes, made load-bearing for the hooks too.
- shfmt enforces from now on, so shell formatting drift fails the commit and CI like every other hook.
- The deviation from the self-pinning convention is deliberate and recorded here so a future contributor does not "fix" it back to hook repos and reintroduce the duplication and flake.

Tracked in #93.
