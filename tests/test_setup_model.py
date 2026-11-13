"""Tests for bin/setup-model.sh, lib.sh's set_dotenv_keys, and offer_model_setup.

Fake `claude` and `codex` binaries stand first on a PATH that holds nothing else
but jq, python3, and the system directories, so no real agent CLI is reached.
Answers are piped on stdin, except where a test needs a terminal and gets a
pseudo-terminal from the pty module.
"""

from __future__ import annotations

import json
import os
import pty
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"
SETUP = REPO_ROOT / "bin" / "setup-model.sh"
TEMPLATE = REPO_ROOT / "templates" / ".env.example"

SECRET = "planted-app-id-4242"
ENV_BODY = f"REPOS=example/example\nGITHUB_APP_ID={SECRET}\n# a comment\r\nMAX_FINDINGS=10\n"
FRESH_BODY = "REPOS=example/example\nREVIEW_HARNESS=\nREVIEW_MODEL=\nREVIEW_EFFORT=high\n"

_FAKE = r"""#!/usr/bin/env python3
import json, os, sys
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
env = os.environ.get
if env("FAKE_CALLS"):
    with open(env("FAKE_CALLS"), "a") as f:
        f.write(" ".join([name] + args) + "\n")
if env("FAKE_STDIN_LOG") and args[:1] not in (["-p"], ["exec"]):
    with open(env("FAKE_STDIN_LOG"), "a") as f:
        f.write(sys.stdin.read())
if name == "claude":
    if args == ["--version"]:
        print(env("FAKE_CLAUDE_VERSION", "999.0.0") + " (Claude Code)")
    elif args[:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": env("FAKE_LOGGED_IN", "1") == "1", "email": "x@example.com"}))
    elif args == ["--help"]:
        print("Options:\n  --model <model>   Model for the session.\n"
              "                    An alias or a full name.\n  -n, --name <name> Other.")
    elif args[:1] == ["-p"]:
        sys.stdin.read()
        if env("FAKE_AGENT_FAIL") == "silent":
            sys.exit(1)
        if env("FAKE_AGENT_FAIL") == "1":
            for i in range(1, 8):
                print(f"agent error line {i}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"type": "result", "result": "OK"}))
    sys.exit(0)
if args[:2] == ["login", "status"]:
    sys.exit(0 if env("FAKE_LOGGED_IN", "1") == "1" else 1)
if args[:2] == ["debug", "models"]:
    if env("FAKE_CATALOG") is None:
        sys.exit(1)
    print(open(env("FAKE_CATALOG")).read())
    sys.exit(0)
if args[:1] == ["exec"]:
    sys.stdin.read()
    if env("FAKE_AGENT_FAIL") == "1":
        for i in range(1, 8):
            print(f"agent error line {i}", file=sys.stderr)
        sys.exit(1)
    with open(args[args.index("-o") + 1], "w") as f:
        f.write("OK")
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
"""

CATALOG = {
    "models": [
        {
            "slug": "example-second",
            "description": "second listed",
            "visibility": "list",
            "priority": 5,
            "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
        },
        {
            "slug": "example-hidden",
            "description": "hidden",
            "visibility": "hide",
            "priority": 1,
            "supported_reasoning_levels": [{"effort": "high"}],
        },
        {
            "slug": "example-first",
            "description": "first listed",
            "visibility": "list",
            "priority": 2,
            "supported_reasoning_levels": [
                {"effort": "medium"},
                {"effort": "high"},
                {"effort": "beyond-max"},
            ],
        },
    ]
}


@pytest.fixture
def base(tmp_path):
    """A working area outside the system temp roots, where run_agent refuses to
    run codex; pytest's tmp_path is under /tmp on a Linux runner."""
    resolved = str(tmp_path.resolve()) + "/"
    if not resolved.startswith(("/tmp/", "/private/tmp/", "/var/tmp/", "/private/var/tmp/")):
        yield tmp_path
        return
    path = Path(tempfile.mkdtemp(dir=Path.home(), prefix=".pr-review-agent-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _path(base: Path, fakes: tuple[str, ...]) -> str:
    fakebin = base / "fakebin"
    toolbin = base / "toolbin"
    for d in (fakebin, toolbin):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir()
    for name in fakes:
        (fakebin / name).write_text(_FAKE)
        (fakebin / name).chmod(0o755)
    for tool in ("jq", "python3"):
        found = shutil.which(tool)
        assert found, f"{tool} is needed to run these tests"
        (toolbin / tool).symlink_to(os.path.realpath(found))
    return os.pathsep.join([str(fakebin), str(toolbin), "/usr/bin", "/bin"])


def _env(base: Path, fakes: tuple[str, ...] = ("claude", "codex"), **extra: str) -> dict:
    (base / "home").mkdir(exist_ok=True)
    (base / "tmp").mkdir(exist_ok=True)
    clean = {
        k: v
        for k, v in os.environ.items()
        if k not in ("REVIEW_HARNESS", "REVIEW_MODEL", "REVIEW_EFFORT", "GITHUB_APP_ID")
    }
    return {
        **clean,
        "PATH": _path(base, fakes),
        "HOME": str(base / "home"),
        "TMPDIR": str(base / "tmp"),
        "PR_REVIEW_STATE_DIR": str(base / "state"),
        **extra,
    }


def _wizard(
    base: Path, answers: list[str], dotenv: Path, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SETUP), str(dotenv)],
        input="".join(f"{a}\n" for a in answers),
        capture_output=True,
        text=True,
        env=env or _env(base),
        timeout=60,
    )


def _dotenv(base: Path) -> Path:
    dotenv = base / ".env"
    dotenv.write_bytes(ENV_BODY.encode())
    return dotenv


def _catalog(base: Path, catalog: dict = CATALOG) -> str:
    path = base / "catalog.json"
    path.write_text(json.dumps(catalog))
    return str(path)


def _lib(script: str, env: dict | None = None, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        _lib_args(script),
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _on_terminal(args: list[str], env: dict, typed: str = "") -> subprocess.CompletedProcess:
    """Runs args with a pseudo-terminal on stdin, the typed lines already queued."""
    master, slave = pty.openpty()
    try:
        os.write(master, typed.encode())
        return subprocess.run(
            args, stdin=slave, capture_output=True, text=True, env=env, timeout=30
        )
    finally:
        os.close(master)
        os.close(slave)


def _lib_args(script: str) -> list[str]:
    return ["bash", "-c", f'set -euo pipefail; source "{LIB}"; {script}']


def _resolve(key: str, dotenv: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k != key}
    return _lib(f'resolve_tunable {key} "{dotenv}"', env).stdout


# set_dotenv_keys


def test_writer_replaces_the_line_resolve_tunable_reads_and_keeps_every_other_byte(tmp_path):
    dotenv = tmp_path / ".env"
    before = b"A=1\r\nREVIEW_MODEL=old\nREVIEW_MODEL=later\nZ=no-trailing-newline"
    dotenv.write_bytes(before)
    result = _lib(f'set_dotenv_keys "{dotenv}" REVIEW_MODEL=example-model[1m]')
    assert result.returncode == 0, result.stderr
    assert dotenv.read_bytes() == before.replace(b"=old", b"=example-model[1m]")
    assert _resolve("REVIEW_MODEL", dotenv) == "example-model[1m]"
    assert dotenv.stat().st_mode & 0o777 == 0o600


def test_writer_appends_a_missing_key(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_bytes(b"A=1\nZ=2")
    assert _lib(f'set_dotenv_keys "{dotenv}" REVIEW_EFFORT=high').returncode == 0
    assert dotenv.read_bytes() == b"A=1\nZ=2\nREVIEW_EFFORT=high\n"
    assert _resolve("REVIEW_EFFORT", dotenv) == "high"


def test_writer_starts_a_missing_dotenv_from_the_template(tmp_path):
    dotenv = tmp_path / ".env"
    result = _lib(f'set_dotenv_keys "{dotenv}" REVIEW_MODEL=example-model')
    assert result.returncode == 0, result.stderr
    template = TEMPLATE.read_text()
    assert dotenv.read_text() == template.replace(
        "\nREVIEW_MODEL=\n", "\nREVIEW_MODEL=example-model\n"
    )


@pytest.mark.parametrize("value", ["has space", "semi;colon", "dollar$x", "quote'", "esc\x1b[2J"])
def test_writer_refuses_a_value_outside_the_alphabet_and_leaves_the_file(tmp_path, value):
    dotenv = tmp_path / ".env"
    dotenv.write_bytes(ENV_BODY.encode())
    result = _lib(f"set_dotenv_keys \"{dotenv}\" REVIEW_EFFORT=high 'REVIEW_MODEL={value}'")
    assert result.returncode != 0
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_writer_writes_an_empty_value_that_resolves_to_the_default(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_bytes(b"REVIEW_MODEL=example-model\n")
    assert _lib(f'set_dotenv_keys "{dotenv}" REVIEW_MODEL=').returncode == 0
    assert dotenv.read_bytes() == b"REVIEW_MODEL=\n"
    env = {k: v for k, v in os.environ.items() if k != "REVIEW_MODEL"}
    default = _lib("resolve_review_model /dev/null", env).stdout
    assert default
    assert _lib(f'resolve_review_model "{dotenv}"', env).stdout == default


def test_writer_refuses_a_read_only_dotenv_and_leaves_it(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_bytes(ENV_BODY.encode())
    dotenv.chmod(0o400)
    result = _lib(f'set_dotenv_keys "{dotenv}" REVIEW_MODEL=example-model')
    assert result.returncode != 0
    assert "not writable" in result.stderr
    assert dotenv.read_bytes() == ENV_BODY.encode()
    assert dotenv.stat().st_mode & 0o777 == 0o400


def test_writer_leaves_no_temporary_file_after_success_or_failure(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_bytes(ENV_BODY.encode())
    assert _lib(f'set_dotenv_keys "{dotenv}" REVIEW_MODEL=example-model').returncode == 0
    assert _lib(f'set_dotenv_keys "{dotenv}" "REVIEW_MODEL=has space"').returncode != 0
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


def test_writer_interrupted_mid_write_leaves_dotenv_and_no_temporary_file(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_bytes(ENV_BODY.encode())
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    started = tmp_path / "started"
    real_python = os.path.realpath(shutil.which("python3"))
    # physical_path still resolves; the write itself hangs until the signal.
    (fakebin / "python3").write_text(
        f'#!/bin/sh\ncase "$2" in *realpath*) exec "{real_python}" "$@" ;; esac\n'
        f'touch "{started}"\nexec sleep 30\n'
    )
    (fakebin / "python3").chmod(0o755)
    env = {**os.environ, "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}"}
    proc = subprocess.Popen(
        _lib_args(f'set_dotenv_keys "{dotenv}" REVIEW_MODEL=example-model'),
        env=env,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 20
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert started.exists()
    os.killpg(proc.pid, signal.SIGTERM)
    proc.wait(timeout=20)
    # The writer's subshell can outlive the shell that started it by its cleanup.
    expected = [".env", "bin", "started"]
    deadline = time.monotonic() + 20
    while sorted(p.name for p in tmp_path.iterdir()) != expected and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sorted(p.name for p in tmp_path.iterdir()) == expected
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_writer_writes_through_a_symlinked_dotenv(tmp_path):
    target = tmp_path / "elsewhere" / "real.env"
    target.parent.mkdir()
    target.write_bytes(ENV_BODY.encode())
    link = tmp_path / ".env"
    link.symlink_to(target)
    assert _lib(f'set_dotenv_keys "{link}" REVIEW_MODEL=example-model').returncode == 0
    assert link.is_symlink()
    assert target.read_bytes() == (ENV_BODY + "REVIEW_MODEL=example-model\n").encode()


# The wizard on claude


def test_claude_run_writes_the_three_keys_and_prints_nothing_else_from_dotenv(base):
    dotenv = _dotenv(base)
    result = _wizard(base, ["claude", "claude-example-model", "medium", "send"], dotenv)
    assert result.returncode == 0, result.stderr
    assert (
        dotenv.read_bytes()
        == (
            ENV_BODY
            + "REVIEW_HARNESS=claude\nREVIEW_MODEL=claude-example-model\nREVIEW_EFFORT=medium\n"
        ).encode()
    )
    assert SECRET not in result.stdout + result.stderr
    assert "claude.ai/settings/data-privacy-controls" in result.stderr
    assert "An alias or a full name." in result.stderr
    assert "next cycle without a restart" in result.stderr
    assert result.stdout == (
        "REVIEW_HARNESS=claude\nREVIEW_MODEL=claude-example-model\nREVIEW_EFFORT=medium\n"
    )


def test_claude_short_name_is_accepted_with_a_note_that_it_may_be_an_alias(base):
    dotenv = _dotenv(base)
    result = _wizard(base, ["claude", "example-alias", "high", "send"], dotenv)
    assert result.returncode == 0, result.stderr
    assert "example-alias is not a full model name; if it is an alias" in result.stderr
    assert _resolve("REVIEW_MODEL", dotenv) == "example-alias"


def test_accepting_the_defaults_writes_empty_values_that_follow_the_daemon_default(base):
    dotenv = _dotenv(base)
    calls = base / "calls.log"
    env = _env(base, FAKE_CALLS=str(calls))
    default_model = _lib("resolve_review_model /dev/null", env).stdout
    default_effort = _lib("resolve_review_effort /dev/null", env).stdout
    result = _wizard(base, ["claude", "", "", "send"], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert (
        dotenv.read_bytes()
        == (ENV_BODY + "REVIEW_HARNESS=claude\nREVIEW_MODEL=\nREVIEW_EFFORT=\n").encode()
    )
    assert f"follow the daemon default, now {default_model}" in result.stderr
    assert _lib(f'resolve_review_model "{dotenv}"', env).stdout == default_model
    assert _lib(f'resolve_review_effort "{dotenv}"', env).stdout == default_effort
    test_call = [c for c in calls.read_text().splitlines() if c.startswith("claude -p")]
    assert len(test_call) == 1
    assert f"--model {default_model} --effort {default_effort}" in test_call[0]


@pytest.mark.parametrize("bad", ["has space", "esc\x1b[2Jape"])
def test_a_bad_model_name_is_asked_again_before_any_call(base, bad):
    dotenv = _dotenv(base)
    calls = base / "calls.log"
    env = _env(base, FAKE_CALLS=str(calls))
    result = _wizard(base, ["claude", bad, "claude-example-model", "high", "send"], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert "A model name holds only" in result.stderr
    test_calls = [c for c in calls.read_text().splitlines() if c.startswith("claude -p")]
    assert len(test_calls) == 1
    assert "--model claude-example-model" in test_calls[0]
    assert _resolve("REVIEW_MODEL", dotenv) == "claude-example-model"


def test_a_bad_model_name_with_no_second_answer_never_reaches_the_cli(base):
    dotenv = _dotenv(base)
    calls = base / "calls.log"
    env = _env(base, FAKE_CALLS=str(calls))
    result = _wizard(base, ["claude", "has space"], dotenv, env)
    assert result.returncode != 0
    assert "claude -p" not in calls.read_text()
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_wizard_refuses_a_read_only_dotenv_before_any_question(base):
    dotenv = _dotenv(base)
    dotenv.chmod(0o400)
    result = _wizard(base, ["claude", "claude-example-model", "high", "send"], dotenv)
    assert result.returncode == 1
    assert "not writable" in result.stderr
    assert "Agent CLI" not in result.stderr
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_shell_environment_override_is_named_after_writing(base):
    dotenv = _dotenv(base)
    env = _env(base, REVIEW_EFFORT="low")
    result = _wizard(base, ["claude", "claude-example-model", "medium", "send"], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert "REVIEW_EFFORT is also set in your shell environment, which overrides .env." in (
        result.stderr
    )
    assert "REVIEW_MODEL is also set" not in result.stderr
    assert "REVIEW_HARNESS is also set" not in result.stderr


def test_wizard_on_a_silent_terminal_stops_at_the_timeout_and_writes_nothing(base):
    dotenv = _dotenv(base)
    env = _env(base, PR_REVIEW_ANSWER_TIMEOUT_SECONDS="1")
    started = time.monotonic()
    result = _on_terminal(["bash", str(SETUP), str(dotenv)], env)
    assert time.monotonic() - started < 15
    assert result.returncode != 0
    assert "no answer within 1 seconds" in result.stderr
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_failed_test_call_with_no_stderr_prints_no_stderr_heading(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_AGENT_FAIL="silent")
    result = _wizard(base, ["claude", "claude-example-model", "high", "send"], dotenv, env)
    assert result.returncode == 1
    assert "did not answer on claude" in result.stderr
    assert "stderr lines" not in result.stderr
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_failed_test_call_shows_the_agent_stderr_and_writes_nothing(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_AGENT_FAIL="1")
    result = _wizard(base, ["claude", "claude-example-model", "high", "send"], dotenv, env)
    assert result.returncode == 1
    assert "claude-example-model did not answer on claude" in result.stderr
    assert "agent error line 7" in result.stderr
    assert "agent error line 2" not in result.stderr
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_declined_test_call_writes_and_says_not_verified(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_AGENT_FAIL="1")
    result = _wizard(base, ["claude", "claude-example-model", "high", "skip"], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert "not verified" in result.stderr
    assert _resolve("REVIEW_MODEL", dotenv) == "claude-example-model"


@pytest.mark.parametrize("answered", range(4))
def test_end_of_input_at_any_prompt_writes_nothing(base, answered):
    dotenv = _dotenv(base)
    answers = ["claude", "claude-example-model", "high", "send"][:answered]
    result = _wizard(base, answers, dotenv)
    assert result.returncode != 0
    assert dotenv.read_bytes() == ENV_BODY.encode()


def _menu_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if re.match(r"  \d+\) ", line)]


CLI_PICKS = {
    "a number": ("1", "claude"),
    "a name": ("claude", "claude"),
    "an empty answer takes the default": ("", "claude"),
    "another number": ("2", "codex"),
    "another name": ("codex", "codex"),
}
REST = {"claude": ["claude-example-model", "high", "send"], "codex": ["1", "high", "send"]}


@pytest.mark.parametrize("case", CLI_PICKS)
def test_cli_menu_takes_a_number_or_a_name_and_lists_each_cli_by_its_product_name(base, case):
    typed, cli = CLI_PICKS[case]
    dotenv = _dotenv(base)
    env = _env(base, FAKE_CATALOG=_catalog(base))
    result = _wizard(base, [typed, *REST[cli]], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert "Agent CLI:\n  1) claude  Claude Code\n  2) codex   Codex CLI\n" in result.stderr
    assert "Agent CLI, a number or a name [claude]: " in result.stderr
    assert _resolve("REVIEW_HARNESS", dotenv) == cli


def test_cli_menu_asks_again_after_an_out_of_range_number_or_an_unknown_name(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_CATALOG=_catalog(base))
    result = _wizard(base, ["3", "0", "example-cli", *["codex", *REST["codex"]]], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("Choose a number or a name from the list.") == 3
    assert _resolve("REVIEW_HARNESS", dotenv) == "codex"


EFFORT_PICKS = {
    "a number": ("2", "medium"),
    "a name": ("xhigh", "xhigh"),
    "an empty answer takes the default": ("", ""),
}


@pytest.mark.parametrize("case", EFFORT_PICKS)
def test_effort_menu_takes_a_number_or_a_name(base, case):
    typed, written = EFFORT_PICKS[case]
    dotenv = _dotenv(base)
    result = _wizard(base, ["claude", "claude-example-model", typed, "send"], dotenv)
    assert result.returncode == 0, result.stderr
    assert "Effort, a number or a name [high]: " in result.stderr
    assert f"REVIEW_EFFORT={written}\n" in result.stdout


def test_effort_menu_asks_again_after_an_out_of_range_number_or_an_unknown_name(base):
    dotenv = _dotenv(base)
    answers = ["claude", "claude-example-model", "6", "example-level", "low", "send"]
    result = _wizard(base, answers, dotenv)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("Choose a number or a name from the list.") == 2
    assert _resolve("REVIEW_EFFORT", dotenv) == "low"


def test_claude_effort_menu_lists_the_daemon_levels_without_descriptions(base):
    dotenv = _dotenv(base)
    levels = _lib('printf %s "$REVIEW_EFFORT_LEVELS"', _env(base)).stdout.split()
    result = _wizard(base, ["claude", "claude-example-model", "", "send"], dotenv)
    assert result.returncode == 0, result.stderr
    menu = "".join(f"  {i}) {level}\n" for i, level in enumerate(levels, 1))
    assert f"Effort levels claude-example-model takes:\n{menu}" in result.stderr


TEST_CALL_PICKS = {
    "an empty answer sends": ("", 1),
    "the number of send": ("1", 1),
    "send": ("send", 1),
    "the number of skip": ("2", 0),
    "skip": ("skip", 0),
}


@pytest.mark.parametrize("case", TEST_CALL_PICKS)
def test_test_call_menu_sends_or_skips_and_writes_either_way(base, case):
    typed, sent = TEST_CALL_PICKS[case]
    dotenv = _dotenv(base)
    calls = base / "calls.log"
    env = _env(base, FAKE_CALLS=str(calls))
    result = _wizard(base, ["claude", "claude-example-model", "high", typed], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert (
        'Test call (the prompt "Reply with OK"):\n'
        "  1) send  Check that claude-example-model answers on claude\n"
        "  2) skip  Write the settings without checking\n"
        "Test call, a number or a name [send]: "
    ) in result.stderr
    assert len(_model_calls(calls)) == sent
    assert ("not verified" in result.stderr) == (not sent)
    assert _resolve("REVIEW_MODEL", dotenv) == "claude-example-model"


def test_test_call_menu_asks_again_after_an_unknown_answer(base):
    dotenv = _dotenv(base)
    calls = base / "calls.log"
    env = _env(base, FAKE_CALLS=str(calls))
    answers = ["claude", "claude-example-model", "high", "y", "3", "skip"]
    result = _wizard(base, answers, dotenv, env)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("Choose a number or a name from the list.") == 2
    assert _model_calls(calls) == []
    assert _resolve("REVIEW_MODEL", dotenv) == "claude-example-model"


# The wizard on codex


def test_codex_menu_lists_visible_models_by_priority_and_limits_effort(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_CATALOG=_catalog(base))
    result = _wizard(base, ["codex", "1", "low", "high", "send"], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert "example-hidden" not in result.stderr
    assert result.stderr.index("1) example-first") < result.stderr.index("2) example-second")
    assert "3) type a model name" in result.stderr
    assert "Effort levels example-first takes:\n  1) medium\n  2) high\n" in result.stderr
    assert "Effort, a number or a name [high]" in result.stderr
    assert "Choose a number or a name from the list." in result.stderr
    assert "Improve the model for everyone" in result.stderr
    assert _resolve("REVIEW_HARNESS", dotenv) == "codex"
    assert _resolve("REVIEW_MODEL", dotenv) == "example-first"
    assert _resolve("REVIEW_EFFORT", dotenv) == "high"


def test_codex_without_a_catalog_takes_a_typed_model(base):
    dotenv = _dotenv(base)
    result = _wizard(base, ["codex", "example-typed", "max", "send"], dotenv)
    assert result.returncode == 0, result.stderr
    assert "type a model name" in result.stderr
    assert _resolve("REVIEW_MODEL", dotenv) == "example-typed"
    assert _resolve("REVIEW_EFFORT", dotenv) == "max"


def test_codex_refuses_a_claude_model(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_CATALOG=_catalog(base))
    result = _wizard(base, ["codex", "claude-example", "high", "send"], dotenv, env)
    assert result.returncode != 0
    assert "is a Claude model" in result.stderr
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_codex_empty_model_is_asked_again_because_codex_has_no_default(base):
    dotenv = _dotenv(base)
    result = _wizard(base, ["codex", "", "example-typed", "high", "send"], dotenv)
    assert result.returncode == 0, result.stderr
    assert "A model name is required." in result.stderr
    assert "Claude model" not in result.stderr
    assert _resolve("REVIEW_MODEL", dotenv) == "example-typed"


def test_codex_with_the_temp_root_under_tmp_stops_before_the_model_question(base):
    dotenv = _dotenv(base)
    calls = base / "calls.log"
    env = _env(base, FAKE_CATALOG=_catalog(base), FAKE_CALLS=str(calls))
    # macOS mktemp -t ignores TMPDIR, so the temp root is moved under /tmp directly.
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp
    fake_mktemp = base / "fakebin" / "mktemp"
    fake_mktemp.write_text(f'#!/bin/sh\nexec "{real_mktemp}" -d /tmp/pr-review-test.XXXXXX\n')
    fake_mktemp.chmod(0o755)
    result = _wizard(base, ["codex", "1", "high", "send"], dotenv, env)
    assert result.returncode == 1
    assert "Codex refuses to run in" in result.stderr
    assert 'export TMPDIR="$HOME/.pr-review-agent/tmp"' in result.stderr
    assert "Model" not in result.stderr
    assert "debug models" not in calls.read_text()
    assert "exec" not in calls.read_text()
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_codex_catalog_drops_unsafe_slugs_strips_control_characters_and_reads_plain_levels(base):
    dotenv = _dotenv(base)
    catalog = base / "catalog.json"
    entry = {"visibility": "list", "priority": 1}
    catalog.write_text(
        json.dumps(
            {
                "models": [
                    {**entry, "slug": "example ok", "description": "a space"},
                    {**entry, "slug": "example-esc\x1b[2J", "description": "an escape"},
                    {**entry, "slug": "example\nline", "description": "a newline"},
                    {
                        **entry,
                        "slug": "example-plain",
                        "description": "plain\x1b]0;title\x07 levels",
                        "supported_reasoning_levels": ["low", "high"],
                    },
                    {
                        **entry,
                        "slug": "example-unreadable",
                        "description": "unreadable levels",
                        "supported_reasoning_levels": 5,
                    },
                ]
            }
        )
    )
    env = _env(base, FAKE_CATALOG=str(catalog))
    result = _wizard(base, ["codex", "1", "high", "send"], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert "\x1b" not in result.stderr and "\x07" not in result.stderr
    assert "example ok" not in result.stderr
    assert "a newline" not in result.stderr
    assert "1) example-plain       plain]0;title levels" in result.stderr
    assert "2) example-unreadable  unreadable levels" in result.stderr
    assert "3) type a model name" in result.stderr
    assert "Effort levels example-plain takes:\n  1) low\n  2) high\n" in result.stderr
    assert _resolve("REVIEW_MODEL", dotenv) == "example-plain"

    result = _wizard(base, ["codex", "2", "max", "send"], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert "  1) low\n  2) medium\n  3) high\n  4) xhigh\n  5) max\n" in result.stderr


def test_codex_effort_menu_shows_each_catalog_description_stripped_of_control_characters(base):
    dotenv = _dotenv(base)
    levels = [
        {"effort": "low", "description": "example low text"},
        "medium",
        {"effort": "high", "description": "example\x1b[31m high\x07 text"},
        {"effort": "beyond-max", "description": "example unused"},
    ]
    entry = {"visibility": "list", "priority": 1, "supported_reasoning_levels": levels}
    catalog = {"models": [{**entry, "slug": "example-model", "description": "example"}]}
    env = _env(base, FAKE_CATALOG=_catalog(base, catalog))
    result = _wizard(base, ["codex", "1", "", "send"], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert (
        "Effort levels example-model takes:\n"
        "  1) low     example low text\n"
        "  2) medium\n"
        "  3) high    example[31m high text\n"
        "Effort, a number or a name [high]: "
    ) in result.stderr
    assert "\x1b" not in result.stderr and "\x07" not in result.stderr
    assert _resolve("REVIEW_EFFORT", dotenv) == "high"


def test_codex_model_and_effort_menus_start_every_description_in_one_column(base):
    dotenv = _dotenv(base)
    levels = [{"effort": e, "description": f"example {e} text"} for e in ("low", "medium")]
    entry = {"visibility": "list", "supported_reasoning_levels": levels}
    catalog = {
        "models": [
            {**entry, "slug": "example-a", "description": "example short", "priority": 1},
            {**entry, "slug": "example-longer-name", "description": "example long", "priority": 2},
        ]
    }
    env = _env(base, FAKE_CATALOG=_catalog(base, catalog))
    result = _wizard(base, ["codex", "1", "", "skip"], dotenv, env)
    assert result.returncode == 0, result.stderr
    lines = _menu_lines(result.stderr)
    assert all(not line.endswith(" ") for line in lines)
    models = [line for line in lines if ") example-" in line]
    efforts = [line for line in lines if line.endswith(" text")]
    assert len(models) == 2 and len(efforts) == 2
    for menu in (models, efforts):
        assert len({line.index("example ") for line in menu}) == 1


# The checks


def _shim_env(base: Path) -> dict:
    env = _env(base, fakes=())
    shim = base / "fakebin" / "codex"
    (base / "fakebin" / "mise").write_text(_FAKE)
    (base / "fakebin" / "mise").chmod(0o755)
    shim.symlink_to(base / "fakebin" / "mise")
    return env


CHECKS = {
    "claude off PATH": (
        "claude",
        lambda b: _env(b, fakes=("codex",)),
        "claude is not on PATH; install Claude Code, then re-run bash bin/setup-model.sh",
    ),
    "claude too old": ("claude", lambda b: _env(b, FAKE_CLAUDE_VERSION="0.0.1"), "claude update"),
    "claude logged out": (
        "claude",
        lambda b: _env(b, FAKE_LOGGED_IN="0"),
        "Claude Code is not logged in; run claude auth login",
    ),
    "codex off PATH": (
        "codex",
        lambda b: _env(b, fakes=("claude",)),
        "codex is not on PATH; install the Codex CLI, then re-run bash bin/setup-model.sh",
    ),
    "codex shim": ("codex", _shim_env, "not a codex binary"),
    "codex logged out": (
        "codex",
        lambda b: _env(b, FAKE_LOGGED_IN="0"),
        "Codex is not logged in; run codex login",
    ),
}


@pytest.mark.parametrize("case", CHECKS)
def test_each_failed_check_says_what_to_do_and_leaves_dotenv(base, case):
    cli, make_env, message = CHECKS[case]
    dotenv = _dotenv(base)
    result = _wizard(base, [cli, "example-model", "high", "send"], dotenv, make_env(base))
    assert result.returncode == 1
    assert message in result.stderr
    assert dotenv.read_bytes() == ENV_BODY.encode()


# offer_model_setup


def _offer(dotenv: Path, env: dict, typed: str) -> subprocess.CompletedProcess:
    """Runs the offer on a terminal, then prints the first line it left unread."""
    script = (
        f'offer_model_setup "{dotenv}"; left=""; IFS= read -t 1 -r left || true; printf %s "$left"'
    )
    return _on_terminal(_lib_args(script), env, typed)


def _fresh(base: Path) -> Path:
    dotenv = base / ".env"
    dotenv.write_text(FRESH_BODY)
    return dotenv


def test_offer_without_a_terminal_logs_a_hint_and_reads_nothing(base):
    dotenv = _fresh(base)
    result = _lib(f'offer_model_setup "{dotenv}"; cat', _env(base), stdin="y\nclaude\n")
    assert result.returncode == 0
    assert "bash bin/setup-model.sh" in result.stderr
    assert result.stdout == "y\nclaude\n"
    assert dotenv.read_text() == FRESH_BODY


SILENT_OFFERS = {
    "no REVIEW_HARNESS line": ("REVIEW_MODEL=\nREVIEW_EFFORT=high\n", {}),
    "REVIEW_HARNESS in the environment": (FRESH_BODY, {"REVIEW_HARNESS": "claude"}),
    "REVIEW_MODEL in the environment": (FRESH_BODY, {"REVIEW_MODEL": "example-model"}),
    "no .env": (None, {}),
}


@pytest.mark.parametrize("terminal", [True, False], ids=["terminal", "no-terminal"])
@pytest.mark.parametrize("case", SILENT_OFFERS)
def test_offer_outside_a_fresh_setup_prints_and_reads_nothing(base, case, terminal):
    body, extra = SILENT_OFFERS[case]
    dotenv = base / ".env"
    if body is not None:
        dotenv.write_text(body)
    env = _env(base, **extra)
    if terminal:
        result = _offer(dotenv, env, "y\n")
        assert result.stdout == "y"
    else:
        result = _lib(f'offer_model_setup "{dotenv}"; cat', env, stdin="y\n")
        assert result.stdout == "y\n"
    assert result.returncode == 0
    assert result.stderr == ""
    assert (dotenv.read_text() if body is not None else None) == body


def test_offer_yes_runs_the_wizard_on_the_terminal(base):
    dotenv = _fresh(base)
    result = _offer(dotenv, _env(base), "y\nclaude\n\n\nskip\n")
    assert result.returncode == 0, result.stderr
    assert "Agent CLI, a number or a name" in result.stderr
    assert dotenv.read_text() == FRESH_BODY.replace(
        "REVIEW_HARNESS=", "REVIEW_HARNESS=claude"
    ).replace("REVIEW_EFFORT=high", "REVIEW_EFFORT=")


def test_offer_no_writes_the_default_harness_and_is_not_asked_again(base):
    dotenv = _fresh(base)
    env = _env(base)
    first = _offer(dotenv, env, "n\n")
    assert first.returncode == 0, first.stderr
    assert "not asked again" in first.stderr
    after_no = FRESH_BODY.replace("REVIEW_HARNESS=", "REVIEW_HARNESS=claude")
    assert dotenv.read_text() == after_no
    second = _offer(dotenv, env, "y\n")
    assert second.returncode == 0
    assert second.stderr == ""
    assert second.stdout == "y"
    assert dotenv.read_text() == after_no


def test_offer_fails_when_the_wizard_fails_so_run_sh_stops(base):
    dotenv = _fresh(base)
    result = _offer(dotenv, _env(base, FAKE_LOGGED_IN="0"), "y\nclaude\n")
    assert result.returncode != 0
    assert "Claude Code is not logged in" in result.stderr
    assert dotenv.read_text() == FRESH_BODY


@pytest.mark.parametrize("typed", ["", "y\n"], ids=["at the offer", "inside the wizard"])
def test_offer_unanswered_continues_on_the_defaults_and_writes_nothing(base, typed):
    dotenv = _fresh(base)
    env = _env(base, PR_REVIEW_OFFER_TIMEOUT_SECONDS="1", PR_REVIEW_ANSWER_TIMEOUT_SECONDS="1")
    started = time.monotonic()
    result = _offer(dotenv, env, typed)
    assert time.monotonic() - started < 15
    assert result.returncode == 0, result.stderr
    assert "continues on the defaults" in result.stderr
    assert dotenv.read_text() == FRESH_BODY


def test_warn_env_drift_does_not_flag_the_empty_template_keys(tmp_path):
    dotenv = tmp_path / ".env"
    shutil.copy(TEMPLATE, dotenv)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("REVIEW_HARNESS", "REVIEW_MODEL", "REVIEW_EFFORT")
    }
    result = _lib(f'warn_env_drift "{dotenv}" "{TEMPLATE}"', env)
    assert result.returncode == 0
    assert result.stderr == ""


# The flag form


def _run(
    base: Path, args: list[str], dotenv: Path, env: dict | None = None, stdin: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SETUP), *args, str(dotenv)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env or _env(base),
        timeout=60,
    )


def _calls(calls: Path) -> list[str]:
    return calls.read_text().splitlines() if calls.exists() else []


def _model_calls(calls: Path) -> list[str]:
    return [c for c in _calls(calls) if c.startswith(("claude -p", "codex exec"))]


def test_flag_form_ignores_answers_waiting_on_a_pipe(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_CATALOG=_catalog(base))
    result = _run(
        base,
        ["--harness", "claude", "--model", "claude-example-model", "--effort", "low"],
        dotenv,
        env,
        stdin="codex\nexample-first\nhigh\nn\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "REVIEW_HARNESS=claude\nREVIEW_MODEL=claude-example-model\nREVIEW_EFFORT=low\n"
    )
    assert _resolve("REVIEW_HARNESS", dotenv) == "claude"
    assert _resolve("REVIEW_EFFORT", dotenv) == "low"


def test_flag_form_on_a_silent_terminal_finishes_without_waiting(base):
    dotenv = _dotenv(base)
    env = _env(base, PR_REVIEW_ANSWER_TIMEOUT_SECONDS="60")
    started = time.monotonic()
    result = _on_terminal(
        ["bash", str(SETUP), "--harness", "claude", "--no-test-call", str(dotenv)], env
    )
    assert time.monotonic() - started < 15
    assert result.returncode == 0, result.stderr
    assert result.stdout == "REVIEW_HARNESS=claude\nREVIEW_MODEL=\nREVIEW_EFFORT=\n"


def test_no_test_call_alone_selects_the_flag_form_and_never_asks(base):
    dotenv = _dotenv(base)
    env = _env(base, PR_REVIEW_ANSWER_TIMEOUT_SECONDS="60")
    started = time.monotonic()
    result = _on_terminal(["bash", str(SETUP), "--no-test-call", str(dotenv)], env)
    assert time.monotonic() - started < 15
    assert result.returncode == 0, result.stderr
    assert result.stdout == "REVIEW_HARNESS=claude\nREVIEW_MODEL=\nREVIEW_EFFORT=\n"


@pytest.mark.parametrize(
    "args",
    [["--check"], ["--list-models", "--harness", "codex"]],
    ids=["check", "list-models"],
)
def test_read_only_modes_on_a_silent_terminal_finish_without_waiting(base, args):
    dotenv = base / ".env"
    dotenv.write_text("REVIEW_HARNESS=codex\nREVIEW_MODEL=example-first\nREVIEW_EFFORT=high\n")
    env = _env(base, FAKE_CATALOG=_catalog(base), PR_REVIEW_ANSWER_TIMEOUT_SECONDS="60")
    started = time.monotonic()
    result = _on_terminal(["bash", str(SETUP), *args, str(dotenv)], env)
    assert time.monotonic() - started < 15
    assert result.returncode == 0, result.stderr
    assert result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--harness", "codex", "--model", "example-first", "--no-test-call"],
        ["--check"],
        ["--list-models", "--harness", "codex"],
    ],
    ids=["flags", "check", "list-models"],
)
def test_cli_started_by_a_non_interactive_mode_reads_no_stdin(base, args):
    dotenv = base / ".env"
    dotenv.write_text("REVIEW_HARNESS=codex\nREVIEW_MODEL=example-first\nREVIEW_EFFORT=high\n")
    stdin_log = base / "stdin.log"
    env = _env(base, FAKE_CATALOG=_catalog(base), FAKE_STDIN_LOG=str(stdin_log))
    result = _run(base, args, dotenv, env, stdin="planted answer\n")
    assert result.returncode == 0, result.stderr
    assert stdin_log.exists()
    assert stdin_log.read_text() == ""


@pytest.mark.parametrize("current", ["codex", ""], ids=["set to codex", "unset"])
def test_flag_form_without_harness_keeps_the_resolved_harness(base, current):
    dotenv = base / ".env"
    dotenv.write_text(f"REVIEW_HARNESS={current}\n")
    result = _run(base, ["--model", "example-typed", "--no-test-call"], dotenv)
    assert result.returncode == 0, result.stderr
    assert _resolve("REVIEW_HARNESS", dotenv) == (current or "claude")


def test_claude_flag_form_without_model_or_effort_writes_empty_keys_that_resolve_to_defaults(
    base,
):
    dotenv = _dotenv(base)
    calls = base / "calls.log"
    env = _env(base, FAKE_CALLS=str(calls))
    default_model = _lib("resolve_review_model /dev/null", env).stdout
    default_effort = _lib("resolve_review_effort /dev/null", env).stdout
    result = _run(base, ["--harness", "claude"], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "REVIEW_HARNESS=claude\nREVIEW_MODEL=\nREVIEW_EFFORT=\n"
    assert "claude.ai/settings/data-privacy-controls" in result.stderr
    assert _lib(f'resolve_review_model "{dotenv}"', env).stdout == default_model
    assert _lib(f'resolve_review_effort "{dotenv}"', env).stdout == default_effort
    test_call = _model_calls(calls)
    assert len(test_call) == 1
    assert f"--model {default_model} --effort {default_effort}" in test_call[0]


def test_codex_flag_form_writes_the_effort_explicitly_even_at_the_default(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_CATALOG=_catalog(base))
    result = _run(base, ["--harness", "codex", "--model", "example-first"], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "REVIEW_HARNESS=codex\nREVIEW_MODEL=example-first\nREVIEW_EFFORT=high\n"
    assert "Improve the model for everyone" in result.stderr


def test_codex_flag_form_refuses_a_default_effort_the_model_does_not_list(base):
    dotenv = _dotenv(base)
    catalog = {
        "models": [
            {
                "slug": "example-edges",
                "visibility": "list",
                "priority": 1,
                "supported_reasoning_levels": [{"effort": "low"}, {"effort": "xhigh"}],
            }
        ]
    }
    env = _env(base, FAKE_CATALOG=_catalog(base, catalog))
    result = _run(base, ["--harness", "codex", "--model", "example-edges"], dotenv, env)
    assert result.returncode == 1
    assert "takes effort low, xhigh, not the default high; pass --effort" in result.stderr
    assert result.stdout == ""
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_flag_form_without_a_dotenv_starts_it_from_the_template(base):
    dotenv = base / ".env"
    result = _run(base, ["--harness", "claude", "--no-test-call"], dotenv)
    assert result.returncode == 0, result.stderr
    expected = TEMPLATE.read_text().replace("\nREVIEW_EFFORT=high\n", "\nREVIEW_EFFORT=\n")
    assert dotenv.read_text() == expected.replace(
        "\nREVIEW_HARNESS=\n", "\nREVIEW_HARNESS=claude\n"
    )


@pytest.mark.parametrize("source", ["environment", "env file"])
def test_flag_form_refuses_an_invalid_current_harness_and_names_where_it_is_set(base, source):
    dotenv = _dotenv(base)
    extra = {}
    if source == "environment":
        extra["REVIEW_HARNESS"] = "bogus"
    else:
        dotenv.write_text(ENV_BODY + "REVIEW_HARNESS=bogus\n")
    body = dotenv.read_bytes()
    result = _run(base, ["--model", "example-typed", "--no-test-call"], dotenv, _env(base, **extra))
    assert result.returncode == 1
    where = "the environment" if source == "environment" else str(dotenv)
    assert f"REVIEW_HARNESS is set to bogus in {where}" in result.stderr
    assert "pass --harness" in result.stderr
    assert result.stdout == ""
    assert dotenv.read_bytes() == body


def test_codex_flag_form_without_model_is_a_usage_error_before_any_cli_call(base):
    dotenv = _dotenv(base)
    calls = base / "calls.log"
    result = _run(base, ["--harness", "codex"], dotenv, _env(base, FAKE_CALLS=str(calls)))
    assert result.returncode == 2
    assert "--model is required with codex" in result.stderr
    assert result.stdout == ""
    assert _calls(calls) == []
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_codex_flag_form_refuses_an_effort_the_model_does_not_list_and_names_those_it_does(base):
    dotenv = _dotenv(base)
    calls = base / "calls.log"
    env = _env(base, FAKE_CATALOG=_catalog(base), FAKE_CALLS=str(calls))
    result = _run(
        base, ["--harness", "codex", "--model", "example-first", "--effort", "low"], dotenv, env
    )
    assert result.returncode == 1
    assert "example-first on codex takes effort medium, high, not low" in result.stderr
    assert result.stdout == ""
    assert _model_calls(calls) == []
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_flag_form_with_no_test_call_writes_and_says_not_verified(base):
    dotenv = _dotenv(base)
    calls = base / "calls.log"
    env = _env(base, FAKE_CALLS=str(calls), FAKE_AGENT_FAIL="1")
    args = ["--harness", "claude", "--model", "claude-example-model", "--no-test-call"]
    result = _run(base, args, dotenv, env)
    assert result.returncode == 0, result.stderr
    assert "claude-example-model on claude is not verified" in result.stderr
    assert _model_calls(calls) == []
    assert _resolve("REVIEW_MODEL", dotenv) == "claude-example-model"


def test_flag_form_failed_test_call_writes_nothing_and_prints_nothing_on_stdout(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_AGENT_FAIL="1")
    result = _run(base, ["--harness", "claude", "--model", "claude-example-model"], dotenv, env)
    assert result.returncode == 1
    assert "claude-example-model did not answer on claude" in result.stderr
    assert result.stdout == ""
    assert dotenv.read_bytes() == ENV_BODY.encode()


BAD_VALUES = {
    "model with a space": ["--harness", "claude", "--model", "has space"],
    "unknown effort": ["--harness", "claude", "--effort", "beyond-max"],
    "unknown harness": ["--harness", "other", "--model", "example-typed"],
    "claude model on codex": ["--harness", "codex", "--model", "claude-example"],
}


@pytest.mark.parametrize("case", BAD_VALUES)
def test_flag_form_refuses_a_bad_value_before_any_cli_call(base, case):
    dotenv = _dotenv(base)
    calls = base / "calls.log"
    result = _run(base, BAD_VALUES[case], dotenv, _env(base, FAKE_CALLS=str(calls)))
    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert result.stdout == ""
    assert _calls(calls) == []
    assert dotenv.read_bytes() == ENV_BODY.encode()


@pytest.mark.parametrize("case", CHECKS)
def test_flag_form_stops_at_the_same_failed_check_as_the_wizard(base, case):
    cli, make_env, message = CHECKS[case]
    dotenv = _dotenv(base)
    args = ["--harness", cli, "--model", "example-model", "--no-test-call"]
    result = _run(base, args, dotenv, make_env(base))
    assert result.returncode == 1
    assert message in result.stderr
    assert result.stdout == ""
    assert dotenv.read_bytes() == ENV_BODY.encode()


# --check


def _check(
    base: Path, body: str, env: dict, *extra: str
) -> tuple[subprocess.CompletedProcess, Path]:
    dotenv = base / ".env"
    dotenv.write_text(body)
    return _run(base, ["--check", *extra], dotenv, env), dotenv


def _temp_root_under_tmp(base: Path) -> None:
    # macOS mktemp -t ignores TMPDIR, so the temp root is moved under /tmp directly.
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp
    fake_mktemp = base / "fakebin" / "mktemp"
    fake_mktemp.write_text(f'#!/bin/sh\nexec "{real_mktemp}" -d /tmp/pr-review-test.XXXXXX\n')
    fake_mktemp.chmod(0o755)


PASSING_CHECKS = {
    "claude": (
        "REVIEW_HARNESS=claude\nREVIEW_MODEL=claude-example-model\nREVIEW_EFFORT=low\n",
        [
            "harness: claude",
            "model: claude-example-model",
            "effort: low",
            "claude on PATH: ok",
            "claude version: ok",
            "claude logged in: ok",
        ],
    ),
    "codex": (
        "REVIEW_HARNESS=codex\nREVIEW_MODEL=example-first\nREVIEW_EFFORT=high\n",
        [
            "harness: codex",
            "model: example-first",
            "effort: high",
            "codex on PATH: ok",
            "codex is the real binary: ok",
            "codex logged in: ok",
            "model set for codex: ok",
            "effort listed for the model: ok",
            "per-PR clones outside the system temp directories: ok",
        ],
    ),
}


@pytest.mark.parametrize("case", PASSING_CHECKS)
def test_check_passing_reports_each_check_and_makes_no_model_call(base, case):
    body, lines = PASSING_CHECKS[case]
    calls = base / "calls.log"
    env = _env(base, FAKE_CALLS=str(calls), FAKE_CATALOG=_catalog(base))
    result, dotenv = _check(base, body, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == lines
    assert _model_calls(calls) == []
    assert dotenv.read_text() == body


CHECK_FAILURES = {
    "claude off PATH": (
        "REVIEW_HARNESS=claude\n",
        lambda b: _env(b, fakes=("codex",)),
        "claude on PATH: failed",
    ),
    "claude logged out": (
        "REVIEW_HARNESS=claude\n",
        lambda b: _env(b, FAKE_LOGGED_IN="0"),
        "claude logged in: failed",
    ),
    "codex with no model": (
        "REVIEW_HARNESS=codex\nREVIEW_MODEL=\n",
        _env,
        "model set for codex: failed",
    ),
    "codex with a claude model": (
        "REVIEW_HARNESS=codex\nREVIEW_MODEL=claude-example\n",
        _env,
        "model set for codex: failed",
    ),
    "codex temp root refused": (
        "REVIEW_HARNESS=codex\nREVIEW_MODEL=example-first\n",
        _env,
        "per-PR clones outside the system temp directories: failed",
    ),
    "harness unknown": ("REVIEW_HARNESS=other\n", _env, "harness is claude or codex: failed"),
    "codex effort the model does not list": (
        "REVIEW_HARNESS=codex\nREVIEW_MODEL=example-first\nREVIEW_EFFORT=low\n",
        lambda b: _env(b, FAKE_CATALOG=_catalog(b)),
        "effort listed for the model: failed",
    ),
}


@pytest.mark.parametrize("case", CHECK_FAILURES)
def test_check_with_one_failure_names_it_exits_non_zero_and_writes_nothing(base, case):
    body, make_env, line = CHECK_FAILURES[case]
    env = make_env(base)
    if case == "codex temp root refused":
        _temp_root_under_tmp(base)
    result, dotenv = _check(base, body, env)
    assert result.returncode == 1
    assert [ln for ln in result.stdout.splitlines() if ln.endswith(": failed")] == [line]
    assert "ERROR" in result.stderr
    assert dotenv.read_text() == body


def test_check_without_a_catalog_does_not_report_the_effort_as_ok(base):
    body = "REVIEW_HARNESS=codex\nREVIEW_MODEL=example-first\nREVIEW_EFFORT=low\n"
    result, _ = _check(base, body, _env(base))
    assert result.returncode == 0, result.stderr
    assert "effort listed for the model: not checked" in result.stdout.splitlines()
    assert "the effort was not checked" in result.stderr


def test_check_with_test_call_makes_exactly_one_model_call(base):
    calls = base / "calls.log"
    env = _env(base, FAKE_CALLS=str(calls))
    result, _ = _check(base, "REVIEW_HARNESS=claude\n", env, "--test-call")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "test call: ok"
    assert len(_model_calls(calls)) == 1


# --list-models


def test_list_models_prints_codex_models_in_menu_order_with_their_usable_levels(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_CATALOG=_catalog(base))
    result = _run(base, ["--list-models", "--harness", "codex"], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "example-first\tmedium,high\nexample-second\tlow,high\n"
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_list_models_leaves_out_a_model_with_no_level_the_daemon_accepts(base):
    catalog = {"models": [*CATALOG["models"]]}
    catalog["models"].append(
        {
            "slug": "example-unusable",
            "visibility": "list",
            "priority": 3,
            "supported_reasoning_levels": [{"effort": "beyond-max"}],
        }
    )
    env = _env(base, FAKE_CATALOG=_catalog(base, catalog))
    result = _run(base, ["--list-models", "--harness", "codex"], _dotenv(base), env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "example-first\tmedium,high\nexample-second\tlow,high\n"


def test_list_models_reads_the_harness_from_the_env_file(base):
    dotenv = base / ".env"
    dotenv.write_text("REVIEW_HARNESS=codex\n")
    result = _run(base, ["--list-models"], dotenv, _env(base, FAKE_CATALOG=_catalog(base)))
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("example-first\t")


def test_list_models_with_a_failing_catalog_command_exits_non_zero_with_a_plain_message(base):
    result = _run(base, ["--list-models", "--harness", "codex"], _dotenv(base))
    assert result.returncode == 1
    assert "codex debug models failed" in result.stderr
    assert result.stdout == ""


def test_list_models_on_claude_prints_the_daemon_default_and_says_there_is_no_list(base):
    env = _env(base)
    result = _run(base, ["--list-models", "--harness", "claude"], _dotenv(base), env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == _lib("resolve_review_model /dev/null", env).stdout + "\n"
    assert "Claude Code has no command that lists models" in result.stderr
    assert "any model name or alias" in result.stderr


# Usage


@pytest.mark.parametrize(
    "args",
    [
        ["--unknown"],
        ["--check", "--model", "example-model"],
        ["--check", "--harness", "claude"],
        ["--check", "--list-models"],
        ["--list-models", "--effort", "high"],
        ["--test-call"],
        ["--model", ""],
        ["extra-path"],
    ],
)
def test_usage_errors_exit_2_and_write_nothing(base, args):
    dotenv = _dotenv(base)
    calls = base / "calls.log"
    result = _run(base, args, dotenv, _env(base, FAKE_CALLS=str(calls)))
    assert result.returncode == 2
    assert "--help" in result.stderr
    assert result.stdout == ""
    assert _calls(calls) == []
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_help_prints_usage_and_exit_statuses_on_stdout(base):
    result = _run(base, ["--help"], _dotenv(base))
    assert result.returncode == 0
    for text in ("--check", "--list-models", "--no-test-call", "usage error", "142"):
        assert text in result.stdout
