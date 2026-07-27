# pr-review-agent

Automated PR review agent that posts as a self-hosted GitHub App. Run it in your terminal: it polls the repos you choose and submits findings as a review under its `<app>[bot]` identity. Optionally install it as a background `launchd` job.

> **Preview release (v0.x).** Works end-to-end on your own repos. Review wording, line anchoring, and re-review skipping are still being polished. Expect quirks until v1.0.

## How it works

1. You run `bash daemon/run.sh` — a loop that drives `daemon/poll.sh` every `POLL_INTERVAL_SECONDS` (default 300), printing progress to the terminal ([ADR 0009](docs/adr/0009-explicit-polling-loop.md), [ADR 0011](docs/adr/0011-foreground-first-operating-model.md)). Optionally install it as a `launchd` `KeepAlive` job to run in the background, restarted on crash, logout, and reboot.
2. The daemon lists open PRs for each watched repo via `gh`.
3. It skips drafts, PRs labeled `no-ai-review`, and PRs whose HEAD SHA was already reviewed.
4. For the rest, it runs two review roles (`.claude/agents/review-agent-code.md` and `review-agent-intent.md`, ADR 0038) via headless Claude Code (`claude -p`) against the diff, and anchors findings to file/line ranges.
5. It submits the findings as a `COMMENT` review under its App bot identity, in one call ([ADR 0036](docs/adr/0036-github-app-identity.md)).
6. You open the PR to read them, and edit or dismiss any you disagree with.

## Why this exists

- Review comments post as a dedicated `<app>[bot]`, distinct from your own GitHub identity, so the daemon's work is never confused with yours.
- Each teammate can install the App on their own repos; multiple independent reviews on the same PR are fine.
- Runs on your laptop as a self-hosted App: polling, so no webhooks and no hosting bill.
- MIT-licensed. Retune via `.claude/agents/review-agent-code.md` (prompt) and `.env` tunables (confidence gate, findings cap).

## Prerequisites

- macOS (Claude Code runs here; the optional background install uses `launchd`).
- A GitHub App you register and own: its App id in `GITHUB_APP_ID` and its private key at `~/.pr-review-agent/app.pem`. The daemon authenticates as the App, so no `gh auth login` is needed ([ADR 0036](docs/adr/0036-github-app-identity.md)). [Register the GitHub App](#register-the-github-app) below walks it end to end.
- [`gh`](https://cli.github.com/) on `PATH` (the daemon drives it with the App token), plus `openssl` and `curl` for the token mint.
- [`claude`](https://claude.com/claude-code) on `PATH`.
- `git`, `jq`, `python3` 3.13+.

Install however you prefer (brew, asdf, mise, pyenv, system package manager). This repo uses [mise](https://mise.jdx.dev/) to pin dev tool versions for contributors; not required to run the daemon.

## Register the GitHub App

Do this once, before the first run: the daemon has no identity of its own until you give it one. It is roughly ten minutes of form-filling on github.com, with no hosting to arrange and no webhook endpoint to expose, and each step below is a field on a page GitHub walks you through.

**1. Open the form.** Go to **Settings → Developer settings → GitHub Apps → New GitHub App** on your personal account ([github.com/settings/apps/new](https://github.com/settings/apps/new)). Name it whatever you like. The name is what the `[bot]` suffix hangs off, and it carries tone rather than function, so attribution rides in the review footer instead ([ADR 0036](docs/adr/0036-github-app-identity.md)). The canonical instance is named `youshallnotmerge`. GitHub requires a homepage URL and never uses it for anything here; your fork's URL will do.

**2. Turn the webhook off.** Uncheck **Webhook → Active** and subscribe to no events. The daemon polls GitHub on a timer ([ADR 0009](docs/adr/0009-explicit-polling-loop.md)), so it needs no public URL for GitHub to call back to, which is what keeps it runnable from a laptop.

**3. Grant four repository permissions.** Under **Repository permissions**, set Contents to *Read and write*, Issues to *Read and write*, Pull requests to *Read and write*, and leave Metadata at *Read-only* (GitHub selects Metadata for you). Everything else stays *No access*.

Pull requests carries the review and its inline findings. Issues carries the Status comment, which is an issue comment on the PR. Metadata is GitHub's mandatory baseline.

Contents is the one worth pausing on, because *write* access to your code is a lot to hand a tool that only reads it. It buys exactly one thing: closing a review thread. GitHub's `resolveReviewThread` mutation rejects an App holding only `pull_requests: write` with `Resource not accessible by integration`, and the coupling appears nowhere in the permission docs ([community discussion 44650](https://github.com/orgs/community/discussions/44650)). Narrowing this back to *read* on least-privilege grounds is a tempting mistake and a quiet one: reviews keep posting, and threads simply stop closing until someone notices days later. CodeRabbit asks for the same read-and-write entry ([its docs](https://docs.coderabbit.ai/platforms/github-com) list read-and-write on "Code, commit statuses, issues, and pull requests"), so this is the category's floor rather than an unusual demand.

What the permission does not buy is any write to your repository. The daemon makes no commits, no branches, and no file edits, and the installation token is attached to one `gh` call at a time rather than exported, so the review agents Claude Code spawns never inherit it ([ADR 0036](docs/adr/0036-github-app-identity.md) decision 5).

If you ever change permissions on an App that is already installed, GitHub holds the change until you approve it at [github.com/settings/installations](https://github.com/settings/installations). Until you do, the App's settings page shows the new permission while its tokens still carry the old set.

**4. Restrict where it can be installed.** Pick **Only on this account**, then create the App.

**5. Download the private key.** On the App's settings page, under **Private keys**, click **Generate a private key**. GitHub downloads a `.pem` file and never shows it again; if you lose it, generate another and delete the old one. Put it where the daemon looks:

```bash
mkdir -p ~/.pr-review-agent
mv ~/Downloads/<downloaded>.private-key.pem ~/.pr-review-agent/app.pem
chmod 600 ~/.pr-review-agent/app.pem
```

Set `APP_KEY_PATH` in the environment to keep it somewhere else.

**6. Install the App on each repo you want reviewed.** Open the **Install App** tab, install it on your account, and choose **Only select repositories**. There is no installation id to copy down: the daemon asks GitHub which installation covers each watched repo ([ADR 0036](docs/adr/0036-github-app-identity.md) decision 4). A watched repo the App is not installed on is named in a warning when `daemon/run.sh` starts and then skipped. That probe runs at startup rather than every cycle, so installing on a new repo mid-run needs a restart to take effect.

**7. Copy the App id into your config.** The **App ID** is on the App's General settings page, a number. It goes in `.env` as `GITHUB_APP_ID`, which the next section covers.

## Install

```bash
git clone https://github.com/<you>/pr-review-agent.git
cd pr-review-agent

# create your config and edit it
# at minimum: REPOS=<owner>/<repo>, GITHUB_APP_ID=<your App id>
cp templates/.env.example .env

# run it — progress prints to the terminal; Ctrl-C to stop
bash daemon/run.sh
```

Each cycle reviews your watched repos every `POLL_INTERVAL_SECONDS` (default 300). To update: Ctrl-C, `git pull`, re-run.

### Run it in the background (optional)

Want it always-on, surviving logout and reboot? Register a `launchd` `KeepAlive` job:

```bash
bash bin/install.sh     # writes the plist into ~/Library/LaunchAgents/ and bootstraps it
bash bin/uninstall.sh   # stop and remove it
```

The background job is invisible and bound to this checkout's working tree, so keep the checkout on `main` — switching its branch silently breaks the running daemon ([ADR 0011](docs/adr/0011-foreground-first-operating-model.md)).

## Configure

One file at the repo root:

- **`.env`** (required): which repos to watch, the id of the App it posts as, and the daemon's tunables. Copy `templates/.env.example` and edit it; every key is documented there, which is why this list does not repeat them.

Watched repos need nothing checked in. The daemon bundles its own agent definitions into each per-PR clone before it reviews ([ADR 0007](docs/adr/0007-operator-bundled-agents.md)); a repo that wants different behaviour can carry its own file at the same path and the bundle leaves it alone.

### Skip a single PR

Label it `no-ai-review` (or whatever you set as `OPT_OUT_LABEL`). The daemon skips it on the next polling cycle.

## Manual operation

```bash
bash daemon/poll.sh                # run one polling cycle by hand
bash daemon/review-pr.sh <pr-url>  # review one PR ad-hoc

# inspecting the optional background (launchd) install — a foreground run shows all this in the terminal already:
tail -f .daemon.log                # follow its log
echo $(( $(date +%s) - $(cat ~/.pr-review-agent/daemon.heartbeat) ))s   # seconds since its last cycle
```

## Submitting a review

Every review submits immediately as a `COMMENT` review, posted by the App under its `<app>[bot]` identity ([ADR 0036](docs/adr/0036-github-app-identity.md)). There is no pending stage and no own-vs-others fork: a bot review has no private draft to protect, so it publishes in one call. Edit or hide it after the fact if needed.

## Replying to findings

Reply inline to a finding and the daemon picks it up on the next polling cycle. It classifies your reply and leaves an Ack reaction on it:

- **Fix claim** ("Done in `abc123`", "Removed"): 👀, then a threaded reply that either confirms the fix against the file at HEAD or pushes back with the specific mismatch.
- **Question or pushback** ("Why flag this?", "This is a false positive"): 👀 ("seen"), then a threaded reply that either stands by the finding with evidence from the file at HEAD or withdraws it as a false positive.
- **Acknowledgment** ("Thanks", "Deferring to V2"): 👍 ("noted"), no text reply.

The reaction lands on your comment from the `<app>[bot]`, so a "bot 👀" is plainly distinct from your own ([ADR 0036](docs/adr/0036-github-app-identity.md)); the shared-login ambiguity the operator-identity model carried here is gone.

## Forking

Reviews post under your own GitHub App, so a fork attributes to whoever runs it with no shared identity. The footer links to the App's page (`github.com/apps/<slug>`), and the slug comes from the App you register and install ([ADR 0036](docs/adr/0036-github-app-identity.md)), not from the clone's git remote.

To run a fork: register your own App, then set its id in `GITHUB_APP_ID` and drop its private key at `~/.pr-review-agent/app.pem` (see [Register the GitHub App](#register-the-github-app)).

## License

[MIT](./LICENSE).
