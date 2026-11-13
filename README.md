# pr-review-agent

Reviews pull requests from your machine. Posts findings as inline comments under a GitHub App you register.

## What it does

- Runs on your Claude Code or Codex sign-in. No hosted service.
- Reviews each PR with two roles: `code` reads the code but not the PR description or commits, and `intent` checks those against the diff.
- Drops findings below a confidence threshold (default 60 of 100). An editor pass removes weak or inaccurate ones.
- Checks a reply's claimed fix against the file at the current commit, then confirms it or names the mismatch.
- Resolves its thread when a later commit fixes the finding. Leaves it open if in doubt.
- Keeps one status comment per PR and sets a `review` check on each reviewed commit, failed while a finding thread is open.
- Ignores the reviewed repository's `.claude/` directory and settings.

A finding from the `intent` role on this repository's PR #310, whose test plan claimed more than the test checked. The author fixed it; the review agent confirmed:

> _🔀 intent_ | _🔴 important_
>
> **`test_callers_reach_pr_metadata_through_the_seam` doesn't guard what the test plan promises: a caller re-adding its own `gh pr view`.**
>
> - The test plan pins "that neither caller carries its own `gh pr view` again," but at HEAD the test asserts only `derive_pr_metadata in text` per caller.
> - A caller that keeps `derive_pr_metadata` and re-adds its own `gh pr view` passes, so the re-fetch regression the plan claims is caught goes unnoticed.
>
> ✅ _Resolved_: confirmed fixed by the author

## Know before you start

- **You register a GitHub App and keep the machine on.** Reviews run only while the daemon runs. Tested on macOS.
- **It polls.** A review starts on the next polling cycle (default 5 minutes) and takes several minutes.
- **It runs an agent over files any PR author wrote.** See [Security](#security).
- **It is a `0.x` preview.** Review wording, line anchoring, and re-review skipping still change.
- **It needs Claude Code or the Codex CLI, `gh`, `jq`, and Python 3.13.** See [Requirements](docs/running.md#requirements).
- **The first polling cycle reviews every open non-draft PR**, `MAX_PARALLEL` at a time. Each review spends agent CLI usage.

## Quick start

1. **Register a GitHub App** ([walkthrough](docs/github-app.md)). You end with its App ID, its private key at `~/.pr-review-agent/app.pem` (mode 0600), five permissions, the webhook off, and an install per watched repository.

2. **Clone, install dependencies, create `.env`.**

   ```bash
   git clone https://github.com/taewanu/pr-review-agent.git
   cd pr-review-agent
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e .
   cp templates/.env.example .env
   ```

3. **Set the two required keys in `.env`.** `REPOS` takes space-separated `owner/repo` pairs.

   ```bash
   REPOS=<owner>/<repo>
   GITHUB_APP_ID=<your-app-id>
   ```

4. **Start the daemon.** Then open a non-draft PR on a watched repository. The first start offers to [choose the agent CLI and model](docs/running.md#choosing-the-agent-cli-and-model); `n` keeps Claude Code and its default model.

   ```bash
   source .venv/bin/activate
   bash daemon/run.sh
   ```

## How to tell it worked

- The terminal prints `reviewing: <pr-url>`, then `cycle done`.
- The PR gets a `review` check, a status comment from `<app-name>[bot]`, and a review with inline findings. With no findings, the check passes and no review posts.

Press Ctrl-C to stop. If nothing appears, see [Troubleshooting](docs/troubleshooting.md).

## Security

The PR's author writes everything in the per-PR clone; everything else on the machine is yours. Below are Claude Code's limits; [Codex's](docs/running.md#codex) differ.

**What a pull request cannot do**

- Configure the review agent. The roles run with `--restricted --strict-mcp-config`: no settings, hooks, MCP servers, or `CLAUDE.md` instructions load from the clone. The `code` role still reads the clone's `CLAUDE.md` and ADRs as conventions.
- Run a shell command outside a fixed set. Claude Code, running unattended, denies any command that needs approval. Read-only commands need none, and the review call's `acceptEdits` mode approves file edits and `mkdir`, `touch`, `rm`, `rmdir`, `mv`, `cp`, and `sed` inside the per-PR clone.
- Read your home directory, the daemon's files, or another PR's clone from a shell command, write in the clone, or reach the network. The roles' commands run in Claude Code's OS sandbox; without it, no review starts.
- Reach the App's token, which goes to one `gh` call at a time, never into the roles' environment.
- Plant files under the daemon's working-file names. The daemon deletes every `.pr-review-*` entry at the clone's root before it writes.

**What this does not protect against**

- Prompt injection. PR text can steer a role into a wrong or empty review. The limits above bound a role's actions, not its words.
- Changes inside the per-PR clone. In the review call, a steered role can overwrite files there with the Write tool, including the other role's output. The sandbox blocks shell writes, not the file tools.
- Reads outside the denied locations. A role's shell command can still read system paths like `/etc`.
- Linux. The sandbox is tested on macOS only.
- Disclosure to the model provider. The PR's files go to your agent CLI's provider, under your account's training settings.

The App holds the [five permissions](docs/github-app.md#steps) ([why Contents: write](docs/github-app.md#why-contents-needs-write-access)). Its private key stays on your machine.

Use "Report a vulnerability" on this repository's Security tab, not a public issue.

## Documentation

- [GitHub App](docs/github-app.md): registration and permissions.
- [Configuration](docs/configuration.md): settings and defaults.
- [Running](docs/running.md): requirements, the agent CLI and model, the `review` check, replies, background mode, one-shot commands, forking.
- [Troubleshooting](docs/troubleshooting.md): errors and fixes.
- [ADRs](docs/adr/): design decisions.

## License

[MIT](./LICENSE).
