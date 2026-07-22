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

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"

APP_ID = "123456"
INSTALLATION_ID = "789012"

# The call under test, spelled once so the snippets stay inside the line limit.
# _gh_token assigns rather than prints, so a caller must not wrap it in $( ):
# command substitution forks and the cache assignment would die with it.
TOKEN_CALL = f'_gh_token {APP_ID} {INSTALLATION_ID}; printf "%s" "$_GH_TOKEN_VALUE"'


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
    # Pins the 60s backdate itself. Asserting only `iat < now` passes with the
    # `- 60` deleted whenever a second boundary happens to tick during the
    # subprocess, so the regression would fail at random instead of always.
    assert now - payload["iat"] >= 60
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


def test_200_without_an_id_is_exit_2_and_logs(tmp_path):
    """A body that parses but carries no `id` skips the unparseable guard.

    rc=2 keeps the repo in the watch list, so the operator needs a line to
    correlate the per-PR failures that follow against.
    """
    key = _app_key(tmp_path)
    bindir = _stub_curl(tmp_path, "{}", http_code="200")
    out = _run(
        f"APP_KEY_PATH={key}; app_installation_id example example {APP_ID} || echo rc=$?",
        path_prefix=bindir,
    )
    assert "rc=2" in out.stdout
    assert "no installation id" in out.stderr


def _stub_curl_per_repo(tmp_path: Path, installed: str) -> Path:
    """A curl that answers 200 for one repo and 404 for every other."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'if [[ "$*" == *"/repos/{installed}/installation"* ]]; then\n'
        "  printf '%s\\n%s' '{\"id\": 1}' '200'\n"
        "else\n"
        "  printf '%s\\n%s' '{\"message\": \"Not Found\"}' '404'\n"
        "fi\n"
    )
    stub.chmod(0o755)
    return bindir


def test_discovery_names_only_uninstalled_repos(tmp_path):
    """The mixed case the function exists for.

    With every repo answering 404 the expected output is the whole input list,
    so appending every probed repo regardless of exit code would also pass.
    """
    key = _app_key(tmp_path)
    bindir = _stub_curl_per_repo(tmp_path, installed="example/one")
    out = _run(
        f"APP_KEY_PATH={key}; discover_missing_installations {APP_ID} example/one example/two",
        path_prefix=bindir,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["example/two"], "the installed repo must not be listed"
    assert "example/two" in out.stderr
    assert "example/one" not in out.stderr


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
    out = _run(f"{stub}; _gh_token {APP_ID} 111; _gh_token {APP_ID} 222")
    assert out.returncode == 0, out.stderr
    assert counter.read_text() == "xx"


def test_unparseable_expiry_still_caches(tmp_path):
    """The one-hour fallback keeps the entry usable, not merely the call working.

    Asserting a single call only proves the token was minted and returned, which
    holds with the fallback deleted. What breaks without it is the second call:
    the entry caches with an empty expiry, which evaluates to 0 in the
    `((now + margin < expiry))` comparison, so every call re-mints.
    """
    stub, counter = _with_counting_mint(tmp_path, "not-a-timestamp")
    out = _run(f"{stub}; {TOKEN_CALL} >/dev/null; {TOKEN_CALL}")
    assert out.returncode == 0, out.stderr
    assert out.stdout == "tok-abc"
    assert counter.read_text() == "x", "the fallback expiry did not survive into the cache"


def test_mint_failure_propagates(tmp_path):
    # Not TOKEN_CALL: its trailing printf would absorb the `||`.
    out = _run(
        "_mint_installation_token() { return 1; }; "
        f"_gh_token {APP_ID} {INSTALLATION_ID} || echo rc=$?"
    )
    assert "rc=1" in out.stdout


def test_failed_mint_leaves_no_stale_token_behind(tmp_path):
    """A caller that forgets the exit code must not read the previous token."""
    stub, _ = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    out = _run(
        f"{stub}; {TOKEN_CALL} >/dev/null; "
        "_mint_installation_token() { return 1; }; "
        f"_gh_token {APP_ID} 999 || true; printf '[%s]' \"$_GH_TOKEN_VALUE\""
    )
    assert out.stdout == "[]", "a failed mint left the prior call's token readable"


def test_token_is_not_exported(tmp_path):
    """The cache must stay a shell variable, invisible to any spawned child.

    ADR 0036 decision 5: the review agents `claude -p` spawns inherit the
    environment, and every agent definition grants unrestricted Bash. A cache
    that exports would hand each of them write access to every installed repo.
    """
    stub, _ = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    out = _run(
        f"{stub}; _gh_token {APP_ID} {INSTALLATION_ID}; env | grep -c 'tok-abc' || echo 'absent'"
    )
    assert "absent" in out.stdout, "the minted token leaked into the environment"


# --- run_with_app_token -----------------------------------------------------

WRAP = f"run_with_app_token {APP_ID} {INSTALLATION_ID}"


def _stub_allowed(tmp_path: Path, name: str = "gh") -> Path:
    """An allowlisted command that reports the token it was handed."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / name
    stub.write_text('#!/usr/bin/env bash\nprintf %s "${GH_TOKEN:-none}"\n')
    stub.chmod(0o755)
    return bindir


def test_wrapper_reaches_the_child_and_not_the_shell(tmp_path):
    stub, _ = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    bindir = _stub_allowed(tmp_path)
    out = _run(
        f"{stub}; {WRAP} gh api x; printf '|%s' \"${{GH_TOKEN:-unset}}\"",
        path_prefix=bindir,
    )
    assert out.returncode == 0, out.stderr
    # The command sees it; the invoking shell never holds it.
    assert out.stdout == "tok-abc|unset"


def test_wrapper_refuses_to_run_when_the_mint_fails(tmp_path):
    """The reason the wrapper exists, pinned.

    `GH_TOKEN="$(_gh_token ...)" cmd` runs cmd with an empty token at exit 0 when
    the mint fails, so gh silently falls back to the operator's stored login and
    posts under the human identity ADR 0036 replaces. The wrapper must abort.
    """
    bindir = _stub_allowed(tmp_path)
    out = _run(
        f"_mint_installation_token() {{ return 1; }}; {WRAP} gh api x || echo rc=$?",
        path_prefix=bindir,
    )
    assert "none" not in out.stdout, "the command ran without a token"
    assert "rc=1" in out.stdout
    assert "refusing to run" in out.stderr


def test_bare_prefix_would_have_failed_open(tmp_path):
    """Documents the trap the wrapper exists to close, so it stays visible.

    If this ever stops holding, bash changed and the wrapper's rationale needs
    revisiting; until then it is why a convention in a comment was not enough.
    """
    out = _run(
        "_mint_installation_token() { return 1; }; "
        f'GH_TOKEN="$(_gh_token {APP_ID} {INSTALLATION_ID})" '
        "bash -c 'echo RAN with [$GH_TOKEN]' || echo rc=$?"
    )
    assert "RAN with []" in out.stdout
    assert "rc=" not in out.stdout, "the bare prefix does not propagate failure"


def test_wrapper_needs_a_command(tmp_path):
    stub, _ = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    out = _run(f"{stub}; {WRAP} || echo rc=$?")
    assert "rc=1" in out.stdout
    assert "needs a command" in out.stderr


@pytest.mark.parametrize(
    "command",
    [
        # The direct mistake.
        "claude -p review",
        # A full path: the check reads the command name, not its spelling.
        "/usr/local/bin/claude -p x",
        # The repo's own idiom, which a ban on `claude` alone lets through
        # because the first word is the timeout wrapper.
        "run_with_timeout 5 claude -p x",
        # An interpreter reaches claude one level down, and the whole subtree
        # would inherit the token.
        "bash -c 'claude -p x'",
        # A script that starts agents 200 lines later. Nothing can read that
        # from the command line, which is why interpreters are refused outright.
        "bash daemon/review-pr.sh",
        # env launders the name past any first-word check.
        "env claude -p x",
    ],
)
def test_only_allowlisted_commands_may_hold_the_token(tmp_path, command):
    """ADR 0036 decision 5, enforced rather than documented.

    Every agent definition grants unrestricted Bash, so a claude call holding
    this token is write access to every installed repository. A ban on the name
    `claude` fails open on every case below except the first two; an allowlist
    refuses all of them, because none of these is `gh` or `python3`.
    """
    stub, _ = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    out = _run(f"{stub}; {WRAP} {command} || echo rc=$?")
    assert "rc=1" in out.stdout
    assert "may hold an App token" in out.stderr


def test_cache_warms_through_the_wrapper(tmp_path):
    """The path that actually runs in production, which no other test covered.

    An earlier revision read `_gh_token` as `$(...)`. Command substitution forks,
    so the cache assignment died in the subshell while only stdout came back, and
    every wrapped call re-minted. `test_token_is_minted_once_and_reused` missed it
    by calling `_gh_token` in-shell.
    """
    stub, counter = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    bindir = _stub_allowed(tmp_path)
    out = _run(
        f"{stub}; {WRAP} gh api x >/dev/null; {WRAP} gh api x >/dev/null; {WRAP} gh api x",
        path_prefix=bindir,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == "tok-abc"
    assert counter.read_text() == "x", "three wrapped calls minted more than once"


def test_python3_may_run_a_helper_but_not_inline_code(tmp_path):
    """python3 is itself an interpreter, so it is admitted only as a .py runner.

    `python3 -c` takes arbitrary code and could spawn an agent as readily as
    bash, which would undo what refusing bash buys.
    """
    stub, _ = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    bindir = _stub_allowed(tmp_path, name="python3")

    allowed = _run(f"{stub}; {WRAP} python3 daemon/resolution.py --thread 1", path_prefix=bindir)
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout == "tok-abc"

    refused = _run(f"{stub}; {WRAP} python3 -c 'import os' || echo rc=$?", path_prefix=bindir)
    assert "rc=1" in refused.stdout
    assert "never inline code" in refused.stderr


def test_outer_timeout_wrapping_works_but_is_not_needed(tmp_path):
    """Pins why no per-call wrapper is offered, so the comment cannot regress.

    This spelling used to exit 127: run_with_timeout was `perl -e '... exec
    @ARGV'`, and exec resolves a program on PATH rather than a shell function.
    #251 dropped the exec, so the wrapper now resolves like any other name. The
    reason none is offered is therefore that it is unnecessary, not impossible:
    a hung call is bounded by poll.sh's run_with_pr_timeout around the dispatch.
    """
    stub, _ = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    bindir = _stub_allowed(tmp_path)
    out = _run(f"{stub}; run_with_timeout 5 {WRAP} gh api x", path_prefix=bindir)
    assert out.returncode == 0, out.stderr
    # The token still reaches the child, so the cap composes with the wrapper.
    assert out.stdout == "tok-abc"


@pytest.mark.parametrize("command", ["git status", "curl https://x", "jq ."])
def test_nothing_else_is_admitted(tmp_path, command):
    """Pins the accepted shapes to gh and .py helpers alone.

    An allowed command is trusted not to spawn an agent itself. Widening that
    trust should fail a test rather than pass quietly, and these are plausible
    things a future caller might reach for.
    """
    stub, _ = _with_counting_mint(tmp_path, "2099-01-01T00:00:00Z")
    out = _run(f"{stub}; {WRAP} {command} || echo rc=$?")
    assert "rc=1" in out.stdout
    assert "may hold an App token" in out.stderr


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


def test_well_formed_error_body_without_a_token_is_rejected(tmp_path):
    """A 4xx that parses cleanly but carries no `token` must not yield a credential.

    The non-JSON case dies at the jq step and returns through the unparseable
    path, so it never reaches the empty-token guard. Without this, deleting that
    guard leaves the suite green while an empty credential prints.
    """
    key = _app_key(tmp_path)
    bindir = _stub_curl(tmp_path, '{"message": "Bad credentials"}')
    out = _run(
        f"APP_KEY_PATH={key}; _mint_installation_token {APP_ID} {INSTALLATION_ID} || echo rc=$?",
        path_prefix=bindir,
    )
    assert "rc=1" in out.stdout
    assert "installation token rejected: Bad credentials" in out.stderr


def test_successful_response_fields_land_in_the_right_order(tmp_path):
    """The real parse, which every caching test stubs past.

    `jq … | @tsv` then `IFS=$'\\t' read -r token expires_at api_message` is only
    exercised here; reordering either side would hand callers the expiry as the
    credential and nothing else would notice.
    """
    key = _app_key(tmp_path)
    bindir = _stub_curl(tmp_path, '{"token": "ghs_real", "expires_at": "2026-07-22T07:08:52Z"}')
    out = _run(
        f"APP_KEY_PATH={key}; _mint_installation_token {APP_ID} {INSTALLATION_ID}",
        path_prefix=bindir,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == "ghs_real 2026-07-22T07:08:52Z"


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
