# Contributing

## Dev environment

Requires [`mise`](https://mise.jdx.dev/) for managed toolchains.

```sh
mise install
pre-commit install
```

`mise install` resolves the pins in `mise.toml` (Python, ruff, shellcheck, shfmt, actionlint, gitleaks, yamllint, pre-commit). `pre-commit install` registers the git hook so checks run on every commit.

## Conventions

- One slice per PR (vertical, not horizontal)
- Squash merges into `main`
- Conventional commit prefixes (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`)
- ADRs in `docs/adr/` for hard-to-reverse decisions

## More

TBD as the project takes shape.
