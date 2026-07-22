"""Tests for daemon/lib.sh's GitHub App authentication helpers (ADR 0036).

The helpers are shell functions, so tests source lib.sh and call them via
`bash -c` under the daemon's own `set -euo pipefail`. Network calls are stubbed
by putting a fake `curl` earlier on PATH.

The load-bearing test here is test_token_is_not_exported. ADR 0036 decision 5
grants the seam its safety: every agent definition allows unrestricted Bash, so
a token reaching a `claude -p` child would be write access to every installed
repository. A cache that leaks into the environment breaks that silently, and
nothing else in the suite would notice.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"

APP_ID = "123456"
INSTALLATION_ID = "789012"

# The call under test, spelled once so the snippets stay inside the line limit.
TOKEN_CALL = f"gh_token {APP_ID} {INSTALLATION_ID}"


def _run(snippet: str, path_prefix: Path | None = None) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ}
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail; source {LIB}; {snippet}"],
        capture_output=True,
        text=True,
        env=env,
    )


def _stub_curl(tmp_path: Path, body: str, http_code: str = "200") -> Path:
    """A fake curl that echoes a fixed body, appending the code when -w is used."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "curl"
    # lib.sh passes -w '\n%{http_code}', so the format arg arrives with a
    # literal backslash-n that real curl expands. Match on the directive itself.
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '  if [[ "$arg" == *"%{http_code}"* ]]; then\n'
        f"    printf '%s\\n%s' '{body}' '{http_code}'\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        f"printf '%s' '{body}'\n"
    )
    stub.chmod(0o755)
    return bindir


def _app_key(tmp_path: Path) -> Path:
    key = tmp_path / "app.pem"
    subprocess.run(
        ["openssl", "genrsa", "-out", str(key), "2048"],
        check=True,
        capture_output=True,
    )
    return key


# --- JWT -------------------------------------------------------------------


def test_jwt_has_three_base64url_segments(tmp_path):
    key = _app_key(tmp_path)
    out = _run(f"APP_KEY_PATH={key}; _app_jwt {APP_ID}")
    assert out.returncode == 0, out.stderr
    segments = out.stdout.split(".")
    assert len(segments) == 3
    # base64url carries no padding and neither of the two substituted characters.
    for segment in segments:
        assert "=" not in segment
        assert "+" not in segment
        assert "/" not in segment


def test_jwt_backdates_iat_and_names_the_app(tmp_path):
    import base64
    import json
    import time

    key = _app_key(tmp_path)
    out = _run(f"APP_KEY_PATH={key}; _app_jwt {APP_ID}")
    payload_b64 = out.stdout.split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))

    now = int(time.time())
    assert payload["iss"] == APP_ID
    # Backdated so a clock running slightly fast is not rejected.
    assert payload["iat"] < now
    # Well inside GitHub's 10-minute ceiling for an App JWT.
    assert payload["exp"] - payload["iat"] <= 600


def test_jwt_fails_loudly_on_a_missing_key(tmp_path):
    out = _run(f"APP_KEY_PATH={tmp_path}/absent.pem; _app_jwt {APP_ID}")
    assert out.returncode != 0
    assert "could not sign" in out.stderr


# --- Installation discovery -------------------------------------------------


def test_installation_id_returned_on_200(tmp_path):
    key = _app_key(tmp_path)
    bindir = _stub_curl(tmp_path, '{"id": 148179165}', http_code="200")
    out = _run(
        f"APP_KEY_PATH={key}; app_installation_id example example {APP_ID}",
        path_prefix=bindir,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == "148179165"


def test_missing_installation_is_exit_1(tmp_path):
    """404 is the skip-with-a-warning case, distinct from a failed probe."""
    key = _app_key(tmp_path)
    bindir = _stub_curl(tmp_path, '{"message": "Not Found"}', http_code="404")
    out = _run(
        f"APP_KEY_PATH={key}; app_installation_id example example {APP_ID} || echo rc=$?",
        path_prefix=bindir,
    )
    assert "rc=1" in out.stdout


def test_probe_failure_is_exit_2(tmp_path):
    """A 5xx is evidence of neither installed nor missing, so it must not be 1."""
    key = _app_key(tmp_path)
    bindir = _stub_curl(tmp_path, '{"message": "Server Error"}', http_code="503")
    out = _run(
        f"APP_KEY_PATH={key}; app_installation_id example example {APP_ID} || echo rc=$?",
        path_prefix=bindir,
    )
    assert "rc=2" in out.stdout


def test_discovery_names_only_uninstalled_repos(tmp_path):
    key = _app_key(tmp_path)
    bindir = _stub_curl(tmp_path, '{"message": "Not Found"}', http_code="404")
    out = _run(
        f"APP_KEY_PATH={key}; discover_missing_installations {APP_ID} example/one example/two",
        path_prefix=bindir,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["example/one", "example/two"]
    assert "App not installed on" in out.stderr


def test_unreachable_repo_stays_in_the_watch_list(tmp_path):
    """A probe that fails outright must not be reported as uninstalled."""
    key = _app_key(tmp_path)
    bindir = _stub_curl(tmp_path, '{"message": "Server Error"}', http_code="503")
    out = _run(
        f"APP_KEY_PATH={key}; discover_missing_installations {APP_ID} example/one",
        path_prefix=bindir,
    )
    assert out.stdout == ""
    assert "App not installed on" not in out.stderr


# --- Token minting and caching ----------------------------------------------


def _with_counting_mint(tmp_path: Path, expires_at: str) -> tuple[str, Path]:
    """A stub mint that tallies its calls, as a single line the snippet can chain."""
    counter = tmp_path / "mint_calls"
    counter.write_text("")
    stub = (
        "_mint_installation_token() { "
        f"printf 'x' >> {counter}; printf 'tok-abc %s' '{expires_at}'; "
        "}"
    )
    return stub, counter


def test_token_is_minted_once_and_reused(tmp_path):
    stub, counter = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    out = _run(f"{stub}; {TOKEN_CALL} >/dev/null; {TOKEN_CALL}")
    assert out.returncode == 0, out.stderr
    assert out.stdout == "tok-abc"
    assert counter.read_text() == "x", "second call should have hit the cache"


def test_expiring_token_is_reminted(tmp_path):
    """A token inside the refresh margin is treated as spent, not reused."""
    stub, counter = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    # Margin wider than the stub's expiry distance forces every call to re-mint.
    out = _run(
        f"GH_TOKEN_REFRESH_MARGIN=99999999999; {stub}; "
        f"{TOKEN_CALL} >/dev/null; {TOKEN_CALL} >/dev/null"
    )
    assert out.returncode == 0, out.stderr
    assert counter.read_text() == "xx"


def test_cache_is_keyed_per_installation(tmp_path):
    """A token minted for one installation is not valid for another."""
    stub, counter = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    out = _run(f"{stub}; gh_token {APP_ID} 111 >/dev/null; gh_token {APP_ID} 222 >/dev/null")
    assert out.returncode == 0, out.stderr
    assert counter.read_text() == "xx"


def test_unparseable_expiry_still_yields_a_token(tmp_path):
    """The token is valid even when its expiry does not parse; the margin absorbs it."""
    stub, _ = _with_counting_mint(tmp_path, "not-a-timestamp")
    out = _run(f"{stub}; gh_token {APP_ID} {INSTALLATION_ID}")
    assert out.returncode == 0, out.stderr
    assert out.stdout == "tok-abc"


def test_mint_failure_propagates(tmp_path):
    out = _run(f"_mint_installation_token() {{ return 1; }}; {TOKEN_CALL} || echo rc=$?")
    assert "rc=1" in out.stdout


def test_token_is_not_exported(tmp_path):
    """The cache must stay a shell variable, invisible to any spawned child.

    ADR 0036 decision 5: the review agents `claude -p` spawns inherit the
    environment, and every agent definition grants unrestricted Bash. A cache
    that exports would hand each of them write access to every installed repo.
    """
    stub, _ = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    out = _run(
        f"{stub}; gh_token {APP_ID} {INSTALLATION_ID} >/dev/null; "
        "env | grep -c 'tok-abc' || echo 'absent'"
    )
    assert "absent" in out.stdout, "the minted token leaked into the environment"


# --- run_with_app_token -----------------------------------------------------

WRAP = f"run_with_app_token {APP_ID} {INSTALLATION_ID}"


def test_wrapper_reaches_the_child_and_not_the_shell(tmp_path):
    stub, _ = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    out = _run(
        f"{stub}; {WRAP} bash -c 'printf %s \"$GH_TOKEN\"'; printf '|%s' \"${{GH_TOKEN:-unset}}\""
    )
    assert out.returncode == 0, out.stderr
    # The command sees it; the invoking shell never holds it.
    assert out.stdout == "tok-abc|unset"


def test_wrapper_refuses_to_run_when_the_mint_fails(tmp_path):
    """The reason the wrapper exists, pinned.

    `GH_TOKEN="$(gh_token ...)" cmd` runs cmd with an empty token at exit 0 when
    the mint fails, so gh silently falls back to the operator's stored login and
    posts under the human identity ADR 0036 replaces. The wrapper must abort.
    """
    out = _run(
        "_mint_installation_token() { return 1; }; "
        f"{WRAP} bash -c 'echo COMMAND_RAN' || echo rc=$?"
    )
    assert "COMMAND_RAN" not in out.stdout, "the command ran without a token"
    assert "rc=1" in out.stdout
    assert "refusing to run" in out.stderr


def test_bare_prefix_would_have_failed_open(tmp_path):
    """Documents the trap the wrapper exists to close, so it stays visible.

    If this ever stops holding, bash changed and the wrapper's rationale needs
    revisiting; until then it is why a convention in a comment was not enough.
    """
    out = _run(
        "_mint_installation_token() { return 1; }; "
        f'GH_TOKEN="$(gh_token {APP_ID} {INSTALLATION_ID})" '
        "bash -c 'echo RAN with [$GH_TOKEN]' || echo rc=$?"
    )
    assert "RAN with []" in out.stdout
    assert "rc=" not in out.stdout, "the bare prefix does not propagate failure"


def test_wrapper_needs_a_command(tmp_path):
    stub, _ = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    out = _run(f"{stub}; {WRAP} || echo rc=$?")
    assert "rc=1" in out.stdout
    assert "needs a command" in out.stderr


# --- Malformed responses ----------------------------------------------------


def test_non_json_mint_response_does_not_abort_the_daemon(tmp_path):
    """An unguarded jq assignment would take the calling script down with set -e."""
    key = _app_key(tmp_path)
    bindir = _stub_curl(tmp_path, "<html>502 Bad Gateway</html>")
    out = _run(
        f"APP_KEY_PATH={key}; _mint_installation_token {APP_ID} {INSTALLATION_ID} "
        "|| echo rc=$?; echo STILL_ALIVE",
        path_prefix=bindir,
    )
    assert "STILL_ALIVE" in out.stdout, "set -e killed the script instead of returning"
    assert "rc=1" in out.stdout


def test_non_json_probe_response_is_exit_2(tmp_path):
    key = _app_key(tmp_path)
    bindir = _stub_curl(tmp_path, "<html>oops</html>", http_code="200")
    out = _run(
        f"APP_KEY_PATH={key}; app_installation_id example example {APP_ID} "
        "|| echo rc=$?; echo STILL_ALIVE",
        path_prefix=bindir,
    )
    assert "STILL_ALIVE" in out.stdout
    assert "rc=2" in out.stdout
