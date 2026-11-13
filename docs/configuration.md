# Configuration

This page lists the daemon's settings with their defaults, and how to skip a single PR. Variables that exist only for the test suite are not listed.

Settings live in `.env` at the repository root. Git ignores the file. `templates/.env.example` documents each key.

`MAX_PARALLEL` and `OPT_OUT_LABEL` are read from `.env` only. Where `REPOS` and `GITHUB_APP_ID` are read depends on the script:

- **The polling cycle reads both from `.env` only.**
- **The startup check in `run.sh` reads both from a shell variable first, then `.env`.** This check logs the repositories the App is not installed on.
- **`review-pr.sh` and `reply-pr.sh` read `GITHUB_APP_ID` from a shell variable first, then `.env`,** when you pass no `--app-id`.

For the other keys, a shell variable overrides `.env`.

| Key | Default | Controls |
|---|---|---|
| `REPOS` | required | repositories to watch, space-separated `owner/repo` |
| `GITHUB_APP_ID` | required | the App to post as |
| `POLL_INTERVAL_SECONDS` | `300` | seconds between polling cycles |
| `MAX_PARALLEL` | `1` (template: `3`) | PRs reviewed at once, 1 to 16 |
| `CLAUDE_SLOT_POOL_SIZE` | `3` (template: `10`) | concurrent `claude` calls, clamped to 16 |
| `OPT_OUT_LABEL` | `no-ai-review` | label that skips a PR |
| `CONFIDENCE_THRESHOLD` | `60` | minimum finding confidence, 0 to 100 |
| `MAX_FINDINGS` | `10` | findings per review |
| `REVIEW_MODEL` | `claude-opus-5-5` | model for every role |
| `REVIEW_EFFORT` | `high` | `low`, `medium`, `high`, `xhigh`, or `max` |

The default applies when a key is absent or empty. The template sets some keys to other values. At startup the daemon logs any template key your `.env` lacks.

A changed value takes effect on the next polling cycle. `POLL_INTERVAL_SECONDS` takes effect when you restart the daemon.

The two concurrency limits fail differently:

- **`MAX_PARALLEL` above 16 is rejected.** Configuration loading fails, and every polling cycle fails until you fix the value.
- **`CLAUDE_SLOT_POOL_SIZE` above 16 is clamped.** The daemon logs the clamp and uses 16.

An invalid `REVIEW_EFFORT` falls back to `high`, with a log line.

## Environment-only settings

These are read from the shell environment only, not from `.env`. Set them in the shell that starts the daemon. The background job cannot set them: it sees only `PATH` and `HOME`. Timeouts are in seconds.

| Variable | Default | Controls |
|---|---|---|
| `APP_KEY_PATH` | `~/.pr-review-agent/app.pem` | the App's private key |
| `PR_REVIEW_STATE_DIR` | `~/.pr-review-agent` | state, locks, heartbeat, and pause files |
| `REVIEW_AGENT_TIMEOUT` | `600` | the review call that runs both roles |
| `EDITOR_AGENT_TIMEOUT` | `600` | the editor pass |
| `FIX_CHECK_AGENT_TIMEOUT` | `180` | each check of whether a later commit fixed a finding |
| `REPLY_AGENT_TIMEOUT` | `300` | the reply pass |
| `PER_PR_TIMEOUT` | `1800` | one PR's review or reply step in a polling cycle |
| `GH_API_CALL_TIMEOUT` | `90` | fetching a PR's diff |
| `RESOLVE_TOUCHED_CAP` | `10` | open findings on changed lines checked for a fix, per review |
| `RESOLVE_UNTOUCHED_CAP` | `5` | other open findings checked for a fix, per review |
| `SESSION_PAUSE_FALLBACK_SECONDS` | `3600` | the pause after a Claude usage limit with no readable reset time |
| `NO_COLOR` | unset | any value turns off colored terminal output |

## Skipping a single PR

Add the `no-ai-review` label, or the label set in `OPT_OUT_LABEL`, to the PR. An empty `OPT_OUT_LABEL` also means `no-ai-review`. The label filter cannot be turned off.
