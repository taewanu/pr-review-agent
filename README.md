# pr-review-agent

Reviews pull requests from your machine. Posts findings as inline comments under a GitHub App you register.

## What it does

- Runs on your Claude Code sign-in. No hosted service.
- Reviews each PR with two roles. `code` reads the code, not the PR description or commits. `intent` checks the description and commits against the diff.
- Drops findings below a confidence threshold (default 60 of 100). An editor pass removes weak or inaccurate ones.
- When a reply claims a fix, checks the file at the current commit. Confirms the fix or names the mismatch.
- Resolves its thread when a later commit fixes the finding. Leaves it open if in doubt.
- Keeps one status comment per PR. Sets a `review` check on each reviewed commit, failed if a finding thread is open.
- Ignores the reviewed repository's `.claude/` directory and settings.

A finding from the `intent` role on this repository's PR #310. The description's test plan claimed more than the test checked. The author fixed it and the review agent confirmed the fix:

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
- **It needs Claude Code, `gh`, `jq`, and Python 3.13.** See [Requirements](docs/running.md#requirements).

## Quick start

1. **Register a GitHub App** ([walkthrough](docs/github-app.md)). At the end you have its App ID, its private key at `~/.pr-review-agent/app.pem` (mode 0600), five permissions, the webhook off, and the App installed on each watched repository.

2. **Clone, install dependencies, create `.env`.**

   ```bash
   git clone https://github.com/taewanu/pr-review-agent.git
   cd pr-review-agent
   python3 -m pip install -e .
   cp templates/.env.example .env
   ```

3. **Set the two required keys in `.env`.** `REPOS` takes space-separated `owner/repo` pairs.

   ```bash
   REPOS=<owner>/<repo>
   GITHUB_APP_ID=<your-app-id>
   ```

4. **Start the daemon.** Then open a non-draft pull request on a watched repository.

   ```bash
   bash daemon/run.sh
   ```

## How to tell it worked

- The terminal prints `reviewing: <pr-url>`, then `cycle done`.
- The PR gets a `review` check, a status comment from `<app-name>[bot]`, and a review with inline findings. With no findings, no review is posted and the check passes.

Press Ctrl-C to stop. If nothing appears, see [Troubleshooting](docs/troubleshooting.md).

## Security

Everything in the per-PR clone is written by the pull request's author. Everything else on the machine is yours.

**What a pull request cannot do**

- Configure the review agent. The roles run with `--restricted --strict-mcp-config`: no settings, hooks, MCP servers, or `CLAUDE.md` instructions load from the clone. The `code` role still reads the clone's `CLAUDE.md` and ADRs as conventions to review against.
- Run a shell command that needs approval. The daemon runs unattended and passes no flag that skips the prompt, so Claude Code refuses it.
- Reach the App's token. It is attached to one `gh` call at a time and never enters the roles' environment.
- Plant files under the daemon's working-file names. Every `.pr-review-*` entry at the clone's root is deleted before the daemon writes.

**What this does not protect against**

- Prompt injection. PR text can steer a role into a wrong or empty review. The limits above bound what a role can do, not what it says.
- Anything Claude Code itself allows. No OS-level sandbox surrounds the roles' commands.
- Disclosure to the model provider. The PR's files go to the provider behind your Claude Code sign-in. Training use follows your account settings.

The App holds the [five permissions](docs/github-app.md#steps) ([why Contents: write](docs/github-app.md#why-contents-needs-write-access)). Its private key stays on your machine at mode 0600.

Report a vulnerability with "Report a vulnerability" on the repository's Security tab, not in a public issue.

## Documentation

- [GitHub App](docs/github-app.md): registration and permissions.
- [Configuration](docs/configuration.md): settings and defaults.
- [Running](docs/running.md): requirements, the `review` check, replies, background mode, one-shot commands, forking.
- [Troubleshooting](docs/troubleshooting.md): errors and fixes.
- [ADRs](docs/adr/): design decisions.

## License

[MIT](./LICENSE).
