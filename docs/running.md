# Running the daemon

This page covers what the daemon needs, choosing the agent CLI and model, how a review runs, running on Codex, foreground and background operation, one-shot commands, replies to findings, and forking.

## Requirements

- **A machine that stays on.** macOS is the tested platform. CI runs the test suite on Linux. The foreground loop runs without `launchd`. Only background mode needs it.
- **Claude Code v2.1.281 or later**, as `claude` on your `PATH`, signed in. The daemon passes role definitions to `--agents` as a file, which v2.1.281 added. The daemon checks the version before each review. To review with the Codex CLI instead, see [Codex](#codex).
- **On Linux, `bubblewrap` and `socat`.** Claude Code's sandbox needs both there. On Ubuntu 26.04, Ubuntu's default AppArmor profile for `bwrap` stops agent shell commands from running in the sandbox, and the AppArmor fixes in Claude Code's sandboxing docs did not lift it in testing.
- **`gh`** ([GitHub CLI](https://cli.github.com/)), **`git`**, **`jq`**, **`openssl`**, and **`curl`** on your `PATH`. You do not need `gh auth login`. The daemon signs in as the App.
- **Python 3.13 or later**, with the dependencies in `pyproject.toml` installed in a virtual environment.
- **A GitHub App you register.** See [Register the GitHub App](github-app.md).

The daemon's scripts run the first `python3` on `PATH`. Create the virtual environment in the checkout with a Python 3.13 or later:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Activate it with `source .venv/bin/activate` in each new shell before you run a daemon script. Homebrew's Python refuses `pip install` outside a virtual environment with `externally-managed-environment`. The `python3` that ships with macOS is 3.9.

If you use mise, `mise install` provides the Python 3.13 pinned in `mise.toml` instead, and you run `pip install -e .` and each daemon script under `mise exec --`.

## Choosing the agent CLI and model

`bin/setup-model.sh` sets `REVIEW_HARNESS`, `REVIEW_MODEL`, and `REVIEW_EFFORT` in `.env`. It checks that the CLI is installed and signed in. For Claude Code it also checks the version; for Codex it checks that the `codex` it runs is the real binary, not a version-manager shim, and that it accepts every flag and config key the daemon sends. It prints how the provider may use what the review agent sends, and can make one test call with the prompt "Reply with OK". It writes nothing unless every step passes. `bash bin/setup-model.sh --help` lists every flag and exit status.

On a fresh `.env`, `run.sh` offers it at the first start in a terminal. To run it yourself:

```bash
bash bin/setup-model.sh
```

It asks for each value. Enter keeps the value shown in brackets.

To set the values without questions, pass flags. With `codex`, list the models first; each line is a model name, a tab, and the effort levels the model takes:

```bash
bash bin/setup-model.sh --list-models --harness codex
bash bin/setup-model.sh --harness codex --model <model>
```

`<model>` is a name from that list. With `claude`, `--model` takes any model name or alias Claude Code accepts, and leaving it out keeps the daemon default. `--effort <level>` sets the effort. `--no-test-call` skips the test call.

To check the current setting without changing it:

```bash
bash bin/setup-model.sh --check
```

It prints the CLI, model, and effort, then one line per check, and exits non-zero if a check failed.

## How it works

Each polling cycle, the daemon lists open PRs on each watched repository. It skips drafts, PRs with the [opt-out label](configuration.md#skipping-a-single-pr), and commits it has already reviewed.

For each remaining PR, the daemon:

1. Makes a per-PR clone.
2. Runs the `code` and `intent` roles through `claude -p`, or `codex exec` under [Codex](#codex) ([ADR 0038](adr/0038-review-roles-from-contamination-isolation.md)).
3. Drops findings below the confidence threshold ([ADR 0022](adr/0022-confidence-scored-generate-verify.md)), then runs the editor pass ([ADR 0016](adr/0016-editorial-pass-over-review-content.md)).
4. Posts the remaining findings as one `COMMENT` review.
5. Concludes the `review` check ([ADR 0039](adr/0039-review-state-on-the-checks-row.md)).

On later commits, the daemon reviews only the new changes. It resolves threads whose findings the new code fixed ([ADR 0017](adr/0017-commit-driven-thread-resolution.md)).

Role definitions come from `.claude/agents/` in the daemon's checkout, not from the per-PR clone ([ADR 0007](adr/0007-operator-bundled-agents.md)). To change how reviews behave, edit those files.

## Codex

With `REVIEW_HARNESS=codex` in `.env`, every agent call runs on the Codex CLI instead of Claude Code.

- **Requirements:** `codex` on your `PATH`, signed in, and `REVIEW_MODEL` set to a model Codex accepts. An unset or `claude-` model fails at startup. Before each review or reply run, the daemon runs `codex exec` once with no prompt and no credentials, so a `codex` that rejects one of the daemon's flags or config keys fails there.
- **Cost:** Codex reports no dollar cost. The routing ledger and `eval/run_eval.py` show it as unknown, and `eval/run_eval.py --max-cost` refuses to run.
- **Roles:** each role runs as its own `codex exec` process, holding one slot of `CLAUDE_SLOT_POOL_SIZE`, instead of as a subagent of one session. Codex's own subagent tools are off.
- **Sandbox:** each role's shell commands can read the per-PR clone, the Codex install, and system paths, write nothing, and reach no network. The clone's skills, rules, and hooks do not load. The sandbox is tested with Codex 0.154.0 on macOS and on Ubuntu 26.04, where Codex runs the system's bubblewrap 0.11.1.
- **Your repo's `AGENTS.md`:** the review agent does not load it as instructions, because it comes from the PR's own head. The `code` role reads it, with `CLAUDE.md`, as your repo's conventions and judges the change against it.
- **Temp directories:** the sandbox denies `/tmp` and `/var/tmp` to every command, so the daemon refuses a per-PR clone under them. Where `mktemp` uses `/tmp`, as on Linux, set `TMPDIR` to a private directory outside the system directories and the Codex install, which the sandbox lets every command read.

## The `review` check

Each review concludes the `review` check:

- **failure** when a finding thread is open after the review.
- **success** when none is open, with "No findings open on this commit".
- **neutral** when the review stopped before reaching a verdict.

After the daemon answers replies, it brings the check and the status comment to the open finding threads, as long as the reviewed commit is still the PR's latest. Each is updated only if it differs, so a check left behind by an earlier failed update is repaired too. A thread resolved or reopened in the GitHub UI, with no reply, shows on both when the daemon next answers a reply or reviews a commit on the PR.

To block merges on the review, make `review` a required status check in the repository's branch protection.

## Replying to findings

The daemon answers any reply to a finding from a user who is not a bot, on the next polling cycle. It checks the reply against the file at the latest commit.

| Reply | Example | Reaction | Answer |
|---|---|---|---|
| Fix claim | "Done in `abc123`" | 👀 | confirms the fix or names the mismatch |
| Question or pushback | "This is a false positive" | 👀 | stands by the finding or withdraws it |
| Acknowledgment | "Thanks" | 👍 | none |

## Foreground

```bash
source .venv/bin/activate
bash daemon/run.sh
```

- **Stop:** press Ctrl-C. The loop exits and releases its lock.
- **Update:** stop it, run `git pull`, and start it again. A pull that changes only `daemon/poll.sh` or the review pipeline takes effect on the next polling cycle without a restart. `run.sh` starts `poll.sh` fresh each cycle.

One loop runs at a time. A second `run.sh` prints `another pr-review loop is already running` and exits.

## What a run looks like

In the terminal:

- `polling loop up (pid …, interval 300s, …)`: the loop started.
- `watched: <owner>/<repo> (app: <id>, …)`: your `.env` loaded.
- `reviewing: <pr-url> (HEAD <sha>)`: a review began.
- `cycle done · 1 repo · 1 reviewed`: the polling cycle finished.

On the pull request:

- A check named `review` appears while the review runs.
- A status comment from `<app-name>[bot]` names the commit under review. It then lists the findings and the commits reviewed.
- A review from `<app-name>[bot]` carries the findings inline.

If none of this happens, see [Troubleshooting](troubleshooting.md).

## Background (optional)

Background mode registers the daemon as a `launchd` job, which needs macOS. Run `install.sh` with the virtual environment active:

```bash
source .venv/bin/activate
bash bin/install.sh
```

This writes `~/Library/LaunchAgents/com.user.pr-review.plist` and starts the same `run.sh` loop. The job is a per-user LaunchAgent. It runs while you are logged in and starts again at login. `launchd` restarts the loop if it exits with an error. Output goes to `.daemon.log`.

The job keeps the `PATH` it was installed with. With the virtual environment active, that `PATH` starts with `.venv/bin`, so the job runs the same `python3`. With mise, run `mise exec -- bash bin/install.sh` instead. Re-run `install.sh` after changing your `PATH`.

To remove the job:

```bash
bash bin/uninstall.sh
```

This keeps the state in `~/.pr-review-agent/`.

Two limits:

- **Keep this checkout on `main`.** The job runs from this working tree, so switching branches changes the running daemon. Develop in a separate clone ([ADR 0011](adr/0011-foreground-first-operating-model.md)).
- **Shell variables do not reach the job.** It sees only `PATH`, `HOME`, and `.env`. Put settings in `.env` and keep the key at `~/.pr-review-agent/app.pem`.

Follow the log:

```bash
tail -f .daemon.log
```

Show the time since the last polling cycle started:

```bash
echo $(( $(date +%s) - $(cat ~/.pr-review-agent/daemon.heartbeat) ))s since last cycle
```

## One-shot commands

These run one step without the loop. `<pr-url>` is a full `https://github.com/<owner>/<repo>/pull/<number>` URL.

| Command | Does |
|---|---|
| `bash daemon/poll.sh` | one polling cycle |
| `bash daemon/review-pr.sh <pr-url>` | review one PR and post the result |
| `bash daemon/review-pr.sh --dry-run <pr-url>` | review one PR, post nothing |
| `bash daemon/reply-pr.sh <pr-url>` | answer pending replies on one PR |

`review-pr.sh` skips a commit it has already reviewed. `--dry-run` reviews it anyway and keeps the per-PR clone, whose path it prints. `--keep-scratch` keeps the per-PR clone on a normal run of either script. The clone sits alone in a directory of its own, so remove that parent directory, not only the clone.

## Forking

A fork posts under its own App. The review footer links to the page of the App the daemon signs in as. [Register your own App](github-app.md) and set `GITHUB_APP_ID`. Two Apps installed on one repository each post their own review on every PR.
