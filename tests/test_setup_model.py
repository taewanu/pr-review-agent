"""Tests for bin/setup-model.sh, lib.sh's set_dotenv_keys, and offer_model_setup.

Fake `claude` and `codex` binaries stand first on a PATH that holds nothing else
but jq, python3, and the system directories, so no real agent CLI is reached.
Answers are piped on stdin, so no terminal is involved.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"
SETUP = REPO_ROOT / "bin" / "setup-model.sh"
TEMPLATE = REPO_ROOT / "templates" / ".env.example"

SECRET = "planted-app-id-4242"
ENV_BODY = f"REPOS=example/example\nGITHUB_APP_ID={SECRET}\n# a comment\r\nMAX_FINDINGS=10\n"

_FAKE = r"""#!/usr/bin/env python3
import json, os, sys
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
env = os.environ.get
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


def _catalog(base: Path) -> str:
    path = base / "catalog.json"
    path.write_text(json.dumps(CATALOG))
    return str(path)


def _lib(script: str, env: dict | None = None, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{LIB}"; {script}'],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


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


@pytest.mark.parametrize("value", ["has space", "semi;colon", "dollar$x", "quote'", ""])
def test_writer_refuses_a_value_outside_the_alphabet_and_leaves_the_file(tmp_path, value):
    dotenv = tmp_path / ".env"
    dotenv.write_bytes(ENV_BODY.encode())
    result = _lib(f"set_dotenv_keys \"{dotenv}\" REVIEW_EFFORT=high 'REVIEW_MODEL={value}'")
    assert result.returncode != 0
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
    result = _wizard(base, ["claude", "claude-example-model", "medium", "y"], dotenv)
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
    assert "next cycle without a restart" in result.stdout


def test_claude_alias_is_accepted_with_a_note(base):
    dotenv = _dotenv(base)
    result = _wizard(base, ["claude", "example-alias", "high", "y"], dotenv)
    assert result.returncode == 0, result.stderr
    assert "example-alias is an alias" in result.stderr
    assert _resolve("REVIEW_MODEL", dotenv) == "example-alias"


def test_failed_test_call_shows_the_agent_stderr_and_writes_nothing(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_AGENT_FAIL="1")
    result = _wizard(base, ["claude", "claude-example-model", "high", "y"], dotenv, env)
    assert result.returncode == 1
    assert "claude-example-model did not answer on claude" in result.stderr
    assert "agent error line 7" in result.stderr
    assert "agent error line 2" not in result.stderr
    assert dotenv.read_bytes() == ENV_BODY.encode()


def test_declined_test_call_writes_and_says_not_verified(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_AGENT_FAIL="1")
    result = _wizard(base, ["claude", "claude-example-model", "high", "n"], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert "not verified" in result.stderr
    assert _resolve("REVIEW_MODEL", dotenv) == "claude-example-model"


@pytest.mark.parametrize("answered", range(4))
def test_end_of_input_at_any_prompt_writes_nothing(base, answered):
    dotenv = _dotenv(base)
    answers = ["claude", "claude-example-model", "high", "y"][:answered]
    result = _wizard(base, answers, dotenv)
    assert result.returncode != 0
    assert dotenv.read_bytes() == ENV_BODY.encode()


# The wizard on codex


def test_codex_menu_lists_visible_models_by_priority_and_limits_effort(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_CATALOG=_catalog(base))
    result = _wizard(base, ["codex", "1", "low", "high", "y"], dotenv, env)
    assert result.returncode == 0, result.stderr
    assert "example-hidden" not in result.stderr
    assert result.stderr.index("1) example-first") < result.stderr.index("2) example-second")
    assert "3) type a model name" in result.stderr
    assert "Effort, one of: medium high [high]" in result.stderr
    assert "Choose one of: medium high" in result.stderr
    assert "Improve the model for everyone" in result.stderr
    assert _resolve("REVIEW_HARNESS", dotenv) == "codex"
    assert _resolve("REVIEW_MODEL", dotenv) == "example-first"
    assert _resolve("REVIEW_EFFORT", dotenv) == "high"


def test_codex_without_a_catalog_takes_a_typed_model(base):
    dotenv = _dotenv(base)
    result = _wizard(base, ["codex", "example-typed", "max", "y"], dotenv)
    assert result.returncode == 0, result.stderr
    assert "type a model name" in result.stderr
    assert _resolve("REVIEW_MODEL", dotenv) == "example-typed"
    assert _resolve("REVIEW_EFFORT", dotenv) == "max"


def test_codex_refuses_a_claude_model(base):
    dotenv = _dotenv(base)
    env = _env(base, FAKE_CATALOG=_catalog(base))
    result = _wizard(base, ["codex", "claude-example", "high", "y"], dotenv, env)
    assert result.returncode != 0
    assert "is a Claude model" in result.stderr
    assert dotenv.read_bytes() == ENV_BODY.encode()


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
    result = _wizard(base, [cli, "example-model", "high", "y"], dotenv, make_env(base))
    assert result.returncode == 1
    assert message in result.stderr
    assert dotenv.read_bytes() == ENV_BODY.encode()


# offer_model_setup


def test_offer_without_a_terminal_logs_a_hint_and_reads_nothing(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("REVIEW_MODEL=\n")
    env = {k: v for k, v in os.environ.items() if k != "REVIEW_MODEL"}
    result = _lib(f'offer_model_setup "{dotenv}"; cat', env, stdin="y\nclaude\n")
    assert result.returncode == 0
    assert "bash bin/setup-model.sh" in result.stderr
    assert result.stdout == "y\nclaude\n"
    assert dotenv.read_text() == "REVIEW_MODEL=\n"


def test_offer_with_a_model_configured_is_silent(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("REVIEW_MODEL=example-model\n")
    env = {k: v for k, v in os.environ.items() if k != "REVIEW_MODEL"}
    result = _lib(f'offer_model_setup "{dotenv}"; cat', env, stdin="y\n")
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == "y\n"


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
