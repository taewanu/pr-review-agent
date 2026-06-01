# pr-review-agent

Automated PR review agent that posts under your own GitHub identity. A macOS `launchd` daemon polls repos you choose, drafts findings as a **Pending** review, and leaves submit/edit/cancel to you.

> **Preview release (v0.x).** Works end-to-end on your own repos. Review wording, line anchoring, and re-review skipping are still being polished. Expect quirks until v1.0.

## How it works

1. After install (`bin/install.sh`), `launchd` fires `daemon/poll.sh` every `POLL_INTERVAL_SECONDS` (default 300).
2. The daemon lists open PRs for each watched repo via `gh`.
3. It skips drafts, PRs labeled `no-ai-review`, and PRs whose HEAD SHA was already reviewed.
4. For the rest, it runs the review agent (`.claude/agents/review-agent-default.md`) via headless Claude Code (`claude -p`) against the diff, and anchors findings to file/line ranges.
5. It posts the findings as a **Pending** review on your GitHub identity.
6. You open the PR and submit, edit, or dismiss.

The daemon never auto-submits. What gets posted under your name is your call.

## Why this exists

- Review comments post under your own GitHub identity, not a bot account.
- Each teammate can run their own daemon; multiple independent reviews on the same PR are fine.
- Runs on your laptop: no webhooks, no GitHub App, no hosting bill.
- MIT-licensed. Retune via `.claude/agents/review-agent-default.md` (prompt) and `.pr-review.yaml` (per-project rules).

## Prerequisites

- macOS (the daemon uses `launchd`).
- [`gh`](https://cli.github.com/) authenticated (`gh auth login`); reviews post under this identity per [ADR 0003](docs/adr/0003-identity-model.md).
- [`claude`](https://claude.com/claude-code) on `PATH`.
- `git`, `jq`, `python3` 3.13+.

Install however you prefer (brew, asdf, mise, pyenv, system package manager). This repo uses [mise](https://mise.jdx.dev/) to pin dev tool versions for contributors; not required to run the daemon.

## Install

```bash
git clone https://github.com/<you>/pr-review-agent.git
cd pr-review-agent

# create your config and edit it
# at minimum: REPOS=<owner>/<repo>, GITHUB_USER=<your gh login>
cp templates/.env.example .env

# register the launchd job (writes plist into ~/Library/LaunchAgents/, runs launchctl load)
bash bin/install.sh
```

The first polling cycle runs within 5 minutes (configurable via `POLL_INTERVAL_SECONDS`). Remove the job with `bash bin/uninstall.sh`.

## Configure

Two files at the repo root:

- **`.env`** (required): repos, GitHub user, poll interval, opt-out label, optional Slack webhook. See `templates/.env.example`.
- **`.pr-review.yaml`** (optional): language, agents, path filters, per-path instructions, max findings. See `templates/.pr-review.example.yaml`.

V1 only reviews PRs in repos where you've checked in the `.claude/agents/` and `.claude/commands/` files (forks of this repo have them by default). See [ADR 0004](docs/adr/0004-own-pr-review-default.md).

### Skip a single PR

Label it `no-ai-review` (or whatever you set as `OPT_OUT_LABEL`). The daemon skips it on the next polling cycle.

## Manual operation

```bash
bash daemon/poll.sh                # run one polling cycle by hand
bash daemon/review-pr.sh <pr-url>  # review one PR ad-hoc
tail -f .daemon.log                # follow the launchd log
```

## Submitting a review

The daemon drafts a **Pending** review; submitting it is your call — and there's one trap. GitHub's "Finish your review" web modal posts an empty `body` when you leave its textarea blank, **silently overwriting the summary the daemon drafted**. Inline comments survive; the summary is gone for good (`PUT`-recovery returns `422`). This is a consequence of posting under your own identity ([ADR 0003](docs/adr/0003-identity-model.md)) — bot-identity tools never reach a human-submit step.

Two ways to submit without losing the summary:

- **API (recommended)** — omitting `body` preserves the drafted summary:
  ```bash
  gh api -X POST repos/:owner/:repo/pulls/:n/reviews/:id/events -f event=COMMENT
  ```
- **Web UI** — type any character into the modal textarea before submitting (pasting the original summary back also works).

If the summary is already wiped, repost it as a regular PR comment — `PUT`-recovery does not work:

```bash
gh pr comment <pr-url> --body-file <summary-file>
```

## Forking

The review footer link and preview-release banner derive from `git remote get-url origin`. Any clone uses its own owner/repo with no config edit.

This errors only when `origin` is missing or doesn't point to `github.com`. Fix: `git remote add origin <github-url>`.

## License

[MIT](./LICENSE).
