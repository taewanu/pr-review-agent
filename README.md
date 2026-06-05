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

On a colleague's PR the daemon never auto-submits: what lands under your name is your call. On your own PRs it submits a `COMMENT` review directly (your code, your words, editable after), per [ADR 0008](docs/adr/0008-own-pr-auto-submit.md).

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

**Your own PRs** auto-submit: the daemon posts a `COMMENT` review directly, no pending stage ([ADR 0008](docs/adr/0008-own-pr-auto-submit.md)). It is your code and your words, so you edit or hide it after the fact rather than vetting it first.

**Others' PRs** stay pending until you submit. Submit with the helper:

```bash
bash daemon/submit-review.sh <pr-url>
```

It submits via the GitHub API (`POST .../reviews/:id/events`), which preserves the drafted summary. Use it rather than GitHub's "Finish your review" web modal, which can blank the summary.

## Replying to findings

Reply inline to a finding and the daemon picks it up on the next polling cycle. It classifies your reply and leaves an Ack reaction on it:

- **Fix claim** ("Done in `abc123`", "Removed"): 👀, then a threaded reply that either confirms the fix against the file at HEAD or pushes back with the specific mismatch.
- **Question or pushback** ("Why flag this?"): 👀 ("seen"). The daemon does not answer questions yet, so the reaction is the only ack for now.
- **Acknowledgment** ("Thanks", "Deferring to V2"): 👍 ("noted"), no text reply.

**Solo-attribution non-goal:** in a single-identity setup ([ADR 0003](docs/adr/0003-identity-model.md)) the daemon and you share one login, so the reaction lands on your own comment. A user token carries no metadata to distinguish "daemon 👀" from "human 👀"; the only true fix is a separate bot identity (a GitHub App), which the operator-identity model rules out, the same blocker as the pickup check-run path. Accepted: 👀 is the least-human self-react, and the sole reader is the operator who configured the daemon.

## Forking

The review footer link and preview-release banner derive from `git remote get-url origin`. Any clone uses its own owner/repo with no config edit.

This errors only when `origin` is missing or doesn't point to `github.com`. Fix: `git remote add origin <github-url>`.

## License

[MIT](./LICENSE).
