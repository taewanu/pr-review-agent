"""Shared App-auth stubs for the integration harnesses that run poll.sh /
reply-pr.sh end to end (ADR 0036).

Those scripts now resolve a GitHub App installation and mint an installation
token before any gh call. The JWT leg is openssl + curl (not gh), so a harness
must supply a readable App key and a curl that answers the installation probe
and the token mint. gh itself stays stubbed per harness; it just receives a
GH_TOKEN it ignores. Bot artifacts (the dedup sentinel, the status comment) are
authored by BOT_LOGIN_REST, so a harness that seeds a prior review must use it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

APP_ID = "4361858"
APP_SLUG = "example"  # neutral fixture slug, not a real App
BOT_LOGIN_REST = f"{APP_SLUG}[bot]"  # REST author login
BOT_LOGIN_GQL = APP_SLUG  # GraphQL author login (no [bot] suffix)
INSTALLATION_ID = "555"

# A curl that answers the two App endpoints lib.sh reaches: GET .../installation
# (via _app_get, which passes -w '%{http_code}', so append the code) and POST
# .../access_tokens (via _mint_installation_token, no -w, body only). Any other
# repo's installation probe 404s, matching an uninstalled repo.
_CURL_STUB = f"""#!/usr/bin/env bash
args="$*"
case "$args" in
  *"/access_tokens"*)
    printf '%s' '{{"token": "test-token", "expires_at": "2099-01-01T00:00:00Z"}}'
    ;;
  *"/installation"*)
    if [[ "$args" == *"%{{http_code}}"* ]]; then
      printf '%s\\n200' '{{"id": "{INSTALLATION_ID}", "app_slug": "{APP_SLUG}"}}'
    else
      printf '%s' '{{"id": "{INSTALLATION_ID}", "app_slug": "{APP_SLUG}"}}'
    fi
    ;;
  *)
    printf '%s\\n404' '{{"message": "Not Found"}}'
    ;;
esac
"""


def install_app_stubs(bindir: Path) -> dict[str, str]:
    """Write a curl stub and a throwaway App key into `bindir`, and return the env
    overrides a harness must apply (APP_KEY_PATH). Pins APP_KEY_PATH to the fake
    key so a test never falls back to the machine's real ~/.pr-review-agent/app.pem
    or reaches the network. The key is generated at runtime, never committed, so
    gitleaks stays quiet."""
    bindir.mkdir(parents=True, exist_ok=True)
    curl = bindir / "curl"
    curl.write_text(_CURL_STUB)
    curl.chmod(0o755)

    key = bindir / "app.pem"
    subprocess.run(
        ["openssl", "genrsa", "-out", str(key), "2048"],
        check=True,
        capture_output=True,
    )
    return {"APP_KEY_PATH": str(key)}
