# Troubleshooting

This page maps the daemon's error messages and common symptoms to their fixes.

| Message or symptom | Fix |
|---|---|
| `missing '<tool>' on PATH` | install the tool, or fix `PATH` |
| `App private key not readable at …` | place the key at `~/.pr-review-agent/app.pem` |
| `.env not found …` | `cp templates/.env.example .env` |
| `GITHUB_APP_ID must be a numeric App id` | copy the App ID, not the name |
| `MAX_PARALLEL must be <= 16` | set `MAX_PARALLEL` to 16 or less |
| `App not installed on: <repo>` | install the App on that repository |
| `ModuleNotFoundError: No module named 'pydantic'` | run `python3 -m pip install -e .` in the checkout |
| `session limit pause active` | wait until the time shown |
| reviews post, threads never resolve | grant Contents: Read and write |
| no `review` check appears | grant Checks: Read and write |

When your Claude usage limit is reached, the daemon pauses polling until the limit resets. If it cannot read the reset time, it pauses for one hour.

After granting a permission to an installed App, [approve the change](github-app.md#changing-permissions). See [Configuration](configuration.md) for the settings.
