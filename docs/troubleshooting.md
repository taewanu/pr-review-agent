# Troubleshooting

This page maps the daemon's error messages and common symptoms to their fixes.

| Message or symptom | Fix |
|---|---|
| `missing '<tool>' on PATH` | install the tool, or fix `PATH` |
| `could not sign the App JWT with <path>`, then an openssl error | place a valid key at `<path>`, by default `~/.pr-review-agent/app.pem` |
| `App private key not readable at …` | place the key at `~/.pr-review-agent/app.pem` |
| `.env not found …` | `cp templates/.env.example .env` |
| `GITHUB_APP_ID must be a numeric App id` | copy the App ID, not the name |
| `MAX_PARALLEL must be <= 16` | set `MAX_PARALLEL` to 16 or less |
| `App not installed on: <repo>` | install the App on that repository |
| `ModuleNotFoundError: No module named 'pydantic'` | activate the virtual environment (`source .venv/bin/activate`), then start the daemon again. See [Requirements](running.md#requirements) |
| `session limit pause active` | wait until the time shown |
| reviews post, threads never resolve | grant Contents: Read and write |
| no `review` check appears | grant Checks: Read and write |

Without a readable key, the daemon prints `could not sign the App JWT` and openssl's error once at startup. It keeps running. Each polling cycle then prints `App private key not readable at …` and `poll cycle exited non-zero`. The next polling cycle after you place the key runs normally, without a restart.

When your Claude usage limit is reached, the daemon pauses polling until the limit resets. If it cannot read the reset time, it pauses for one hour.

After granting a permission to an installed App, [approve the change](github-app.md#changing-permissions). See [Configuration](configuration.md) for the settings.
