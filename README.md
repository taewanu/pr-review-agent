# pr-review-agent

A pull-request review agent that runs on your machine and posts findings as inline review comments under a GitHub App you register.

## What it does

- Runs on your machine under your own Claude Code sign-in. There is no hosted service.
- Reviews each PR with two agents: `code` reads the diff and surrounding code but not the PR description or commit messages; `intent` compares the description and commit messages with the diff.
- Drops findings scored below a confidence threshold (60 of 100 by default), then has an editor pass re-read the PR and remove weak or inaccurate ones.
- Re-reads the file at the current commit when the author replies that a finding is fixed, then confirms the fix or names the mismatch.
- Resolves its own thread when a later commit fixes the finding without a reply, and leaves it open if the fix is in doubt.
- Keeps one status comment per PR listing the findings and the commits reviewed, and sets a `review` check that fails while a finding thread is open.
- Ignores the reviewed repository's `.claude/` directory and settings.

Finding from the `intent` agent on this repository's PR #310. The test plan in the description claimed more than the test checked; the author fixed it and the review agent confirmed the fix:

> _🔀 intent_ | _🔴 important_
>
> **`test_callers_reach_pr_metadata_through_the_seam` doesn't guard what the test plan promises: a caller re-adding its own `gh pr view`.**
>
> - The test plan pins "that neither caller carries its own `gh pr view` again," but at HEAD the test asserts only `derive_pr_metadata in text` per caller.
> - A caller that keeps `derive_pr_metadata` and re-adds its own `gh pr view` passes, so the re-fetch regression the plan claims is caught goes unnoticed.
>
> ✅ _Resolved_: confirmed fixed by the author

Written in bash and Python, with about 950 tests. Design decisions are in [`docs/adr/`](docs/adr/).

## Know before you start

- **You register your own GitHub App and keep a Mac on.** Reviews happen only while the daemon runs.
- **It polls.** A review starts on the next polling cycle (every 5 minutes by default) and takes several minutes.
- **It is a `0.x` preview.** Review wording, line anchoring, and re-review skipping are still being polished.

## What you need

- **macOS.** The optional background mode uses `launchd`, the macOS service manager.
- **Claude Code v2.1.281 or later**, as `claude` on your `PATH` and signed in. The daemon runs every agent with `--restricted` (added in v2.1.248) and passes role definitions to `--agents` as a file (added in v2.1.281).
- **`gh`** ([GitHub CLI](https://cli.github.com/)), **`git`**, **`jq`**, **`openssl`**, and **`curl`** on your `PATH`. You do not need `gh auth login`: the daemon signs in as the App.
- **Python 3.13 or later** as `python3`, with the `pydantic` package (version 2) installed for it.
- **A GitHub App you register yourself**, about ten minutes on github.com (step 1 below).

The repo's `mise.toml` pins tools for contributors; the daemon does not need mise.

## Quick start

### 1. Register a GitHub App

The App is the identity the daemon posts as, as `<name>[bot]`.

1. Open [github.com/settings/apps/new](https://github.com/settings/apps/new). Pick any name. GitHub requires a homepage URL but never uses it here; your fork's URL will do.
2. Uncheck **Webhook → Active**. The daemon polls, so nothing needs a public URL.
3. Under **Repository permissions**, grant these five and leave everything else at *No access*:

   | Permission | Access | Used for |
   |---|---|---|
   | Checks | Read and write | the `review` check on the PR |
   | Contents | Read and write | resolving review threads ([why](#why-contents-needs-write-access)) |
   | Issues | Read and write | the status comment |
   | Pull requests | Read and write | the review and its findings |
   | Metadata | Read-only | GitHub's required baseline |

4. Choose **Only on this account** and create the App.
5. Note the **App ID** on the App's **General** page.
6. On the same page, click **Generate a private key** and move the downloaded file where the daemon looks:

   ```bash
   mkdir -p ~/.pr-review-agent
   mv ~/Downloads/<downloaded>.private-key.pem ~/.pr-review-agent/app.pem
   chmod 600 ~/.pr-review-agent/app.pem
   ```

7. Open **Install App**, choose **Only select repositories**, and pick the repos to review. There is no installation ID to copy.

### 2. Clone and configure

```bash
git clone https://github.com/<owner>/pr-review-agent.git
cd pr-review-agent
cp templates/.env.example .env
```

In `.env`, set the two required keys. `REPOS` is a space-separated list of `owner/repo` pairs.

```bash
REPOS=<owner>/<repo>
GITHUB_APP_ID=<your-app-id>
```

### 3. Start the daemon

```bash
bash daemon/run.sh
```

It runs in the foreground. Open a non-draft pull request on a watched repo.

## How to tell it worked

**In the terminal:**

- `polling loop up (pid …, interval 300s, …)`: the loop started.
- `watched: <owner>/<repo> (app: <id>, …)`: your `.env` loaded.
- `reviewing: <pr-url> (HEAD <sha>)`: a review began.
- `cycle done · 1 repo · 1 reviewed`: the polling cycle finished.

**On the pull request:**

- A check named `review` appears while the review runs.
- A status comment from `<app-name>[bot]` names the commit under review, then lists the findings.
- A review from `<app-name>[bot]` carries the findings inline. With no findings, no review is posted and the `review` check passes with "No findings open on this commit".

If none of this happens, see [Troubleshooting](#troubleshooting).

## Stop and update

- **Stop:** press Ctrl-C. The loop exits cleanly and releases its lock.
- **Update:** stop it, run `git pull`, and start it again. A pull that changes only `daemon/poll.sh` or the review pipeline takes effect on the next polling cycle without a restart, because `run.sh` launches `poll.sh` fresh each cycle.

---

## How it works

Each polling cycle, the daemon lists open PRs on each watched repo and skips drafts, PRs labeled `no-ai-review`, and commits it has already reviewed. For each remaining PR it makes a per-PR clone, runs the `code` and `intent` roles through `claude -p` ([ADR 0038](docs/adr/0038-review-roles-from-contamination-isolation.md)), applies the confidence gate and editor pass ([ADR 0022](docs/adr/0022-confidence-scored-generate-verify.md), [ADR 0016](docs/adr/0016-editorial-pass-over-review-content.md)), posts the survivors as one `COMMENT` review, and concludes the `review` check ([ADR 0039](docs/adr/0039-review-state-on-the-checks-row.md)). On later commits it reviews only the new changes and resolves threads the new code fixed ([ADR 0017](docs/adr/0017-commit-driven-thread-resolution.md)).

Every agent runs as `claude -p --restricted`, which loads no settings, hooks, or `CLAUDE.md` from the per-PR clone; role definitions come from the daemon's own `.claude/agents/` ([ADR 0007](docs/adr/0007-operator-bundled-agents.md)). To change how reviews behave, edit those files in the daemon's checkout.

### Replying to findings

Reply inline to a finding and the daemon answers on the next polling cycle, checking your claim against the file at the latest commit:

| Your reply | Example | Reaction | Answer |
|---|---|---|---|
| Fix claim | "Done in `abc123`" | 👀 | confirms the fix or names the mismatch |
| Question or pushback | "This is a false positive" | 👀 | stands by the finding or withdraws it |
| Acknowledgment | "Thanks" | 👍 | none |

## Configuration

All settings live in `.env` at the repository root, which git ignores; `templates/.env.example` documents each key. `REPOS`, `GITHUB_APP_ID`, `MAX_PARALLEL`, and `OPT_OUT_LABEL` are read from `.env` only. For the other keys, a shell variable overrides `.env`.

| Key | Default | Controls |
|---|---|---|
| `REPOS` | required | repos to watch |
| `GITHUB_APP_ID` | required | the App to post as |
| `POLL_INTERVAL_SECONDS` | `300` | seconds between cycles |
| `MAX_PARALLEL` | `1` (template: `3`) | PRs reviewed at once, capped at 16 |
| `CLAUDE_SLOT_POOL_SIZE` | `3` (template: `10`) | concurrent `claude` calls |
| `OPT_OUT_LABEL` | `no-ai-review` | label that skips a PR |
| `CONFIDENCE_THRESHOLD` | `60` | minimum finding confidence, 0 to 100 |
| `MAX_FINDINGS` | `10` | findings per review |
| `REVIEW_MODEL` | `claude-opus-5-5` | model for every agent |
| `REVIEW_EFFORT` | `high` | `low` to `max` |

"Default" applies when the key is absent; the template ships some keys with other values. On startup the daemon names any template key your `.env` lacks.

`APP_KEY_PATH`, read from the environment only, moves the private key from `~/.pr-review-agent/app.pem`.

To skip a single PR, add the `no-ai-review` label (or your `OPT_OUT_LABEL`).

## Run in the background (optional)

To keep the daemon running across logout and reboot, register it as a `launchd` job:

```bash
bash bin/install.sh
```

This writes `~/Library/LaunchAgents/com.user.pr-review.plist` and starts the same `run.sh` loop, restarted by `launchd` if it exits with an error. Output goes to `.daemon.log`. `bash bin/uninstall.sh` removes the job and keeps the state in `~/.pr-review-agent/`. Re-run `install.sh` after changing your `PATH`; the job keeps the one it was installed with.

Two limits:

- **Keep this checkout on `main`.** The job runs from this working tree, so switching branches changes the running daemon. Develop in a separate clone ([ADR 0011](docs/adr/0011-foreground-first-operating-model.md)).
- **Shell variables do not reach it.** The job sees only `PATH`, `HOME`, and `.env`, so put settings in `.env` and keep the key at the default path.

Check on it:

```bash
tail -f .daemon.log
echo $(( $(date +%s) - $(cat ~/.pr-review-agent/daemon.heartbeat) ))s since last cycle
```

The heartbeat is stamped at the start of each polling cycle. Only one loop runs at a time; a second `run.sh` prints `another pr-review loop is already running` and exits.

## One-shot commands

These run one step without the loop. `<pr-url>` is a full `https://github.com/<owner>/<repo>/pull/<number>` URL.

| Command | Does |
|---|---|
| `bash daemon/poll.sh` | one polling cycle |
| `bash daemon/review-pr.sh <pr-url>` | review one PR and post the result |
| `bash daemon/review-pr.sh --dry-run <pr-url>` | review one PR, post nothing |
| `bash daemon/reply-pr.sh <pr-url>` | answer pending replies on one PR |

`review-pr.sh` skips a commit it has already reviewed; `--dry-run` reviews it anyway and keeps the per-PR clone, whose path it prints. `--keep-scratch` keeps the clone on a normal run too.

## Troubleshooting

| Message or symptom | Fix |
|---|---|
| `missing '<tool>' on PATH` | install the tool, or fix `PATH` |
| `App private key not readable at …` | place the key at `~/.pr-review-agent/app.pem` |
| `.env not found …` | `cp templates/.env.example .env` |
| `GITHUB_APP_ID must be a numeric App id` | copy the App ID, not the name |
| `App not installed on: <repo>` | install the App on that repo |
| `ModuleNotFoundError: No module named 'pydantic'` | install `pydantic` for `python3` |
| `session limit pause active` | wait; polling resumes at the time shown |
| reviews post, threads never resolve | grant Contents: Read and write |
| no `review` check appears | grant Checks: Read and write |

- **A repo you install the App on later** is picked up on the next polling cycle.
- **After changing an installed App's permissions**, approve the change at [github.com/settings/installations](https://github.com/settings/installations). Until you do, the App's tokens carry the old set.
- **When your Claude usage limit is reached**, the daemon pauses polling until the limit resets (or one hour, if it cannot read the reset time) instead of failing every PR.

### Why Contents needs write access

Contents: write is needed only to close review threads. GitHub's `resolveReviewThread` mutation rejects an App that holds only Pull requests: write, with `Resource not accessible by integration`, and the permission docs do not mention it ([community discussion 44650](https://github.com/orgs/community/discussions/44650)). With read access, reviews post but threads never close.

The permission never writes to your repository. The daemon makes no commits, branches, or file edits, and attaches the App's short-lived installation token to one `gh` call at a time instead of exporting it, so the review agents never see it ([ADR 0036](docs/adr/0036-github-app-identity.md), decision 5).

## Forking

A fork posts under its own App; the review footer links to the page of the App the daemon signs in as. Register your own App (step 1 of the [Quick start](#quick-start)), set `GITHUB_APP_ID`, and place its key at `~/.pr-review-agent/app.pem`. Two Apps installed on one repo produce two independent reviews on every PR.

## License

[MIT](./LICENSE).
