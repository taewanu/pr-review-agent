# Configuration

This page lists every setting the daemon reads, with its default, and how to skip a single PR.

Settings live in `.env` at the repository root. Git ignores the file. `templates/.env.example` documents each key.

`REPOS`, `GITHUB_APP_ID`, `MAX_PARALLEL`, and `OPT_OUT_LABEL` are read from `.env` only. For the other keys, a shell variable overrides `.env`.

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

`APP_KEY_PATH` sets the private key's location. The default is `~/.pr-review-agent/app.pem`. It is read from the environment only, not from `.env`.

## Skipping a single PR

Add the `no-ai-review` label, or the label set in `OPT_OUT_LABEL`, to the PR. An empty `OPT_OUT_LABEL` also means `no-ai-review`. The label filter cannot be turned off.
