"""Tests for the codex side of daemon/lib.sh's run_agent and its role fan-out.

A fake `codex` first on PATH records the argv, stdin, and working directory it
received, prints `--json` events, and writes the final message to its `-o` file,
so a test reads what reached the binary and what the caller was handed back
without a real agent. Mirrors agent_fixture.py's fake `claude`. One test runs
real commands under the sandbox profile with an installed codex, and skips
without one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON = REPO_ROOT / "daemon"
LIB = DAEMON / "lib.sh"
REVIEW_PR = DAEMON / "review-pr.sh"
REPLY_PR = DAEMON / "reply-pr.sh"
TEMPLATE = REPO_ROOT / "templates" / ".env.example"
AGENTS = REPO_ROOT / ".claude" / "agents"

sys.path.insert(0, str(DAEMON))
import codex_exec  # noqa: E402

TIMEOUT_EXIT = 142
MODEL = "example-model"
EFFORT = "high"
PROMPT = "the prompt body\nsecond line\n"
RESULT = "the agent's final text"
SYSTEM = 'a "quoted" word\nback\\slash and a tab\there\nnon-ascii ✓ 😀\n'
AGENT = "review-agent-editor"

_FAKE_CODEX = """#!/usr/bin/env python3
import json, os, subprocess, sys, time
argv, stdin = sys.argv[1:], sys.stdin.read()
with open(os.environ["FAKE_CODEX_LOG"], "a") as f:
    f.write(json.dumps({"exe": sys.argv[0], "argv": argv, "stdin": stdin,
                        "cwd": os.getcwd()}) + "\\n")
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type": "thread.started", "thread_id": "t1"})
emit({"type": "turn.started"})
# Like codex, each command starts in a process group of its own.
pidfile = os.environ.get("FAKE_CODEX_CHILD_PIDFILE")
if pidfile:
    child = subprocess.Popen(["sleep", "300"], process_group=0)
    with open(pidfile, "w") as f:
        f.write(str(child.pid))
# A command whose child double-forks: the grandchild leaves codex's tree but
# keeps the command's group.
orphan_pidfile = os.environ.get("FAKE_CODEX_ORPHAN_PIDFILE")
if orphan_pidfile:
    subprocess.Popen(
        ["sh", "-c", '(sleep 300 & echo $! >"$1"); sleep 300', "_", orphan_pidfile],
        process_group=0,
        stdout=subprocess.DEVNULL,
    )
# A command that escapes every kill (setsid, then out of the tree) and keeps
# codex's stdout open.
held_pidfile = os.environ.get("FAKE_CODEX_HOLD_STDOUT_PIDFILE")
if held_pidfile:
    subprocess.run(
        ["sh", "-c", 'sleep 300 & echo $! >"$1"', "_", held_pidfile], start_new_session=True
    )
hang_on = os.environ.get("FAKE_CODEX_HANG_ON")
if hang_on is not None and hang_on in stdin:
    time.sleep(300)
emit({"type": "item.completed", "item": {"type": "command_execution",
      "command": "cat in.txt", "aggregated_output": "x", "exit_code": 0}})
fail_on = os.environ.get("FAKE_CODEX_FAIL_ON")
if fail_on is not None and fail_on in stdin:
    emit({"type": "turn.failed", "error": {"message": "fixture failure"}})
    sys.exit(int(os.environ.get("FAKE_CODEX_RC", "1")))
text = os.environ.get("FAKE_CODEX_RESULT", stdin)
emit({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
with open(argv[argv.index("-o") + 1], "w") as f:
    f.write(text)
emit({"type": "turn.completed", "usage": {"input_tokens": 100,
      "cached_input_tokens": 40, "output_tokens": 7}})
"""


def _under_tmp(path: Path) -> bool:
    resolved = str(path.resolve()) + "/"
    return resolved.startswith(("/tmp/", "/private/tmp/", "/var/tmp/", "/private/var/tmp/"))


@pytest.fixture
def base(tmp_path):
    """A working area outside the system temp roots, which run_agent refuses under
    codex. pytest's tmp_path is under /tmp wherever TMPDIR is unset, as on a Linux
    CI runner."""
    if not _under_tmp(tmp_path):
        yield tmp_path
        return
    path = Path(tempfile.mkdtemp(dir=Path.home(), prefix=".pr-review-agent-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _install_fake(base: Path, name: str = "codex") -> Path:
    bindir = base / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / name
    fake.write_text(_FAKE_CODEX)
    fake.chmod(0o755)
    return bindir


def _bash(base: Path, script: str, args: list[str], env: dict | None = None, cwd=None):
    bindir = _install_fake(base)
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{LIB}"; {script}', "_", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_CODEX_LOG": str(base / "codex-calls.jsonl"),
            "PR_REVIEW_STATE_DIR": str(base / "state"),
            "CLAUDE_SLOT_POLL_SECONDS": "0.1",
            **(env or {}),
        },
        # A broken kill path would block in `wait` forever.
        timeout=60,
    )


def _calls(base: Path) -> list[dict]:
    log = base / "codex-calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


_RUN_IN_CLONE = (
    f"REVIEW_HARNESS=codex; REVIEW_MODEL={MODEL}; REVIEW_EFFORT={EFFORT}; "
    'cd "$1"; shift; rc=0; run_agent "$@" || rc=$?; exit "$rc"'
)
# Called the way the daemon's call sites call it: from inside a clone whose
# reserved names were cleared right after the checkout.
_RUN = f'clear_reserved_names "$1"; {_RUN_IN_CLONE}'

_FAST_TIMEOUT = {"TIMEOUT_POLL_INTERVAL": "1", "TIMEOUT_KILL_GRACE": "1"}


def _body(agent: str) -> str:
    """An agent file's body, the system prompt a directly-prompted call gets."""
    return (AGENTS / f"{agent}.md").read_text().split("---\n", 2)[2]


def _run_agent(
    base: Path,
    mode="read-only",
    *,
    agent=AGENT,
    timeout_secs=30,
    clone=None,
    env=None,
    clear=True,
):
    clone = clone or base / "clone"
    clone.mkdir(exist_ok=True)
    (clone / "prompt.txt").write_text(PROMPT)
    raw = clone / ".pr-review-raw.txt"
    args = [str(clone), mode, str(timeout_secs), "prompt.txt", str(raw), "pr1:label"]
    if agent:
        args.append(agent)
    script = _RUN if clear else _RUN_IN_CLONE
    result = _bash(base, script, args, env={"FAKE_CODEX_RESULT": RESULT, **(env or {})})
    return result, raw


def _path_without_codex() -> str:
    """This PATH minus every directory holding a codex, so a test that plants its
    own can never reach an installed one."""
    entries = os.environ["PATH"].split(os.pathsep)
    return os.pathsep.join(e for e in entries if not (Path(e) / "codex").exists())


def _config(argv: list[str]) -> dict:
    """Every `-c key=value` the argv carries, each value parsed as codex parses it."""
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    return {k: tomllib.loads(f"v={v}")["v"] for k, v in (p.split("=", 1) for p in pairs)}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int, secs: float = 5) -> bool:
    deadline = time.monotonic() + secs
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not _alive(pid)


def test_read_only_sends_the_sandboxed_argv_with_the_prompt_on_stdin(base):
    result, raw = _run_agent(base)
    assert result.returncode == 0, result.stderr
    [call] = _calls(base)
    clone = str((base / "clone").resolve())
    fake = (base / "bin" / "codex").resolve()
    assert call["exe"] == str(fake)
    assert call["stdin"] == PROMPT
    # Each `-c` value reduced to its key here; the values are asserted parsed below.
    argv = call["argv"]
    shape = [
        a.split("=", 1)[0] if prev == "-c" else a
        for prev, a in zip(["", *argv[:-1]], argv, strict=True)
    ]
    assert shape == [
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        clone,
        "-m",
        MODEL,
        "-c",
        "permissions.pr-review-agent",
        "-c",
        "shell_environment_policy.inherit",
        "-c",
        "shell_environment_policy.set",
        "-c",
        "default_permissions",
        "-c",
        "model_reasoning_effort",
        "-c",
        "developer_instructions",
        "-c",
        "project_doc_max_bytes",
        "-c",
        "skills.include_instructions",
        "-c",
        "skills.bundled.enabled",
        "-c",
        "plugins._default.enabled",
        "--disable",
        "apps",
        "--disable",
        "plugins",
        "--disable",
        "hooks",
        "--disable",
        "image_generation",
        "-c",
        "web_search",
        "-c",
        "approval_policy",
        "-c",
        "allow_login_shell",
        "--json",
        "-o",
        str(raw),
        "-",
    ]
    config = _config(argv)
    # What the profile actually lets a command do is asserted by running commands
    # under it, in test_the_profile_confines_real_commands.
    assert config["permissions.pr-review-agent"]["network"] == {"enabled": False}
    assert config["permissions.pr-review-agent"]["filesystem"][str(fake.parent.parent)] == "read"
    assert config["shell_environment_policy.inherit"] == "core"
    assert config["default_permissions"] == "pr-review-agent"
    assert config["model_reasoning_effort"] == EFFORT
    # The clone's AGENTS.md is the PR author's text; a zero budget loads none of it.
    assert config["project_doc_max_bytes"] == 0
    assert config["skills.include_instructions"] is False
    assert config["skills.bundled.enabled"] is False
    assert config["plugins._default.enabled"] is False
    assert config["web_search"] == "disabled"
    assert config["approval_policy"] == "never"
    assert config["allow_login_shell"] is False
    # --sandbox would make codex ignore the profile.
    assert "--sandbox" not in argv and "-s" not in argv


def test_system_prompt_is_the_daemon_definitions_body(base):
    # The clone is the PR head, so a definition it carries is the PR author's.
    planted = base / "clone" / ".claude" / "agents" / f"{AGENT}.md"
    planted.parent.mkdir(parents=True)
    planted.write_text("---\nname: planted\n---\nPLANTED INSTRUCTIONS\n")
    result, raw = _run_agent(base)
    assert result.returncode == 0, result.stderr
    [call] = _calls(base)
    system = _config(call["argv"])["developer_instructions"]
    assert system == Path(f"{raw}.system.md").read_text()
    assert system.strip() == _body(AGENT).strip()


def test_system_prompt_reaches_developer_instructions_byte_for_byte(base):
    bindir = _install_fake(base)
    system = base / "sys.md"
    system.write_text(SYSTEM)
    argv = codex_exec.build_argv(
        str(bindir / "codex"), str(base), str(base / "raw"), MODEL, EFFORT, str(system)
    )
    assert _config(argv)["developer_instructions"] == SYSTEM


def test_unknown_agent_refuses_without_running_codex(base):
    result, _ = _run_agent(base, agent="review-agent-missing")
    assert result.returncode == 1
    assert "no agent definition" in result.stderr
    assert _calls(base) == []


def test_no_system_prompt_sends_no_developer_instructions(base):
    result, _ = _run_agent(base, agent=None)
    assert result.returncode == 0, result.stderr
    [call] = _calls(base)
    assert "developer_instructions" not in _config(call["argv"])


def test_final_message_lands_in_raw_out_with_a_token_sidecar_and_no_cost(base):
    result, raw = _run_agent(base)
    assert result.returncode == 0, result.stderr
    assert raw.read_text() == RESULT
    # Cached input is part of input, so it is not added again.
    assert Path(f"{raw}.tokens").read_text() == "107"
    # Codex reports no cost, and a fabricated $0 would read as a free review.
    assert not Path(f"{raw}.cost").exists()
    assert "[pr1:label] → cat in.txt" in result.stderr
    assert f"[pr1:label] {RESULT}" in result.stderr


def test_error_exit_surfaces_with_empty_output(base):
    result, raw = _run_agent(base, env={"FAKE_CODEX_FAIL_ON": "", "FAKE_CODEX_RC": "1"})
    assert result.returncode == 1
    assert raw.read_text() == ""
    assert "turn.failed: fixture failure" in result.stderr


def test_turn_failed_surfaces_even_when_codex_exits_zero(base):
    result, _ = _run_agent(base, env={"FAKE_CODEX_FAIL_ON": "", "FAKE_CODEX_RC": "0"})
    assert result.returncode != 0


def test_timeout_kills_the_shell_commands_codex_started(base):
    pidfile = base / "child.pid"
    # The cap includes the fake's interpreter startup, which must reach the pidfile.
    result, raw = _run_agent(
        base,
        timeout_secs=3,
        env={"FAKE_CODEX_CHILD_PIDFILE": str(pidfile), "FAKE_CODEX_HANG_ON": "", **_FAST_TIMEOUT},
    )
    assert result.returncode == TIMEOUT_EXIT
    assert raw.read_text() == ""
    assert not Path(f"{raw}.tokens").exists()
    assert _wait_gone(int(pidfile.read_text())), "a command codex started outlived the timeout"


def test_timeout_kills_a_grandchild_that_double_forked_out_of_the_tree(base):
    pidfile = base / "orphan.pid"
    result, _ = _run_agent(
        base,
        timeout_secs=2,
        env={"FAKE_CODEX_ORPHAN_PIDFILE": str(pidfile), "FAKE_CODEX_HANG_ON": "", **_FAST_TIMEOUT},
    )
    assert result.returncode == TIMEOUT_EXIT
    orphan = int(pidfile.read_text())
    try:
        assert _wait_gone(orphan), "a double-forked grandchild outlived the timeout"
    finally:
        if _alive(orphan):
            os.kill(orphan, 9)


def test_an_outer_cap_kills_a_grandchild_that_double_forked_out_of_the_tree(base):
    # The per-PR cap wraps the script that runs codex under a far longer inner
    # cap, so the outer kill takes down the inner watchdog before its group kill.
    pidfile = base / "orphan.pid"
    clone = base / "clone"
    clone.mkdir()
    (clone / "prompt.txt").write_text(PROMPT)
    args = [str(clone), "read-only", "300", "prompt.txt", str(clone / ".pr-review-raw.txt")]
    result = _bash(
        base,
        'rc=0; run_with_timeout 3 bash -c "$INNER" _ "$@" || rc=$?; exit "$rc"',
        [*args, "pr1:label", AGENT],
        env={
            "INNER": f'set -euo pipefail; source "{LIB}"; {_RUN}',
            "FAKE_CODEX_ORPHAN_PIDFILE": str(pidfile),
            "FAKE_CODEX_HANG_ON": "",
            **_FAST_TIMEOUT,
        },
    )
    assert result.returncode == TIMEOUT_EXIT, result.stderr
    orphan = int(pidfile.read_text())
    try:
        assert _wait_gone(orphan), "a double-forked grandchild outlived the outer cap"
    finally:
        if _alive(orphan):
            os.kill(orphan, 9)


def test_an_escaped_process_holding_stdout_cannot_hold_the_run_past_its_cap(base):
    pidfile = base / "held.pid"
    started = time.monotonic()
    try:
        result, _ = _run_agent(
            base,
            timeout_secs=3,
            env={
                "FAKE_CODEX_HOLD_STDOUT_PIDFILE": str(pidfile),
                "FAKE_CODEX_HANG_ON": "",
                **_FAST_TIMEOUT,
            },
        )
        elapsed = time.monotonic() - started
    finally:
        held = int(pidfile.read_text())
    try:
        assert result.returncode == TIMEOUT_EXIT
        # The escaped process lives for 300s; the run must not wait on it.
        assert _alive(held)
        assert elapsed < 20, f"run took {elapsed:.0f}s with a held stdout"
    finally:
        os.kill(held, 9)


def test_refuses_a_clone_under_a_system_temp_root(base):
    for root in ("/tmp", "/var/tmp"):
        clone = Path(tempfile.mkdtemp(dir=root))
        try:
            result, _ = _run_agent(base, clone=clone)
        finally:
            shutil.rmtree(clone, ignore_errors=True)
        assert result.returncode == 1, root
        assert "TMPDIR" in result.stderr
    assert _calls(base) == []


def test_orchestrate_refuses_and_says_to_run_each_role_read_only(base):
    result, _ = _run_agent(base, mode="orchestrate", agent=None)
    assert result.returncode == 1
    assert "each review role as its own read-only agent" in result.stderr
    assert _calls(base) == []


def _plant_shim(base: Path) -> Path:
    """A version-manager shim: a `codex` link to the manager, whose real path is not codex."""
    bindir = _install_fake(base, name="manager")
    shim_dir = base / "shims"
    shim_dir.mkdir()
    (shim_dir / "codex").symlink_to(bindir / "manager")
    return shim_dir


def test_refuses_when_every_codex_on_path_is_a_shim(base):
    shim_dir = _plant_shim(base)
    result, _ = _run_agent(base, env={"PATH": f"{shim_dir}{os.pathsep}{_path_without_codex()}"})
    assert result.returncode == 1
    assert "not a codex binary" in result.stderr
    assert _calls(base) == []


def test_a_shim_ahead_of_the_real_codex_on_path_is_skipped(base):
    shim_dir = _plant_shim(base)
    bindir = _install_fake(base)
    path = os.pathsep.join((str(shim_dir), str(bindir), _path_without_codex()))
    result, _ = _run_agent(base, env={"PATH": path})
    assert result.returncode == 0, result.stderr
    [call] = _calls(base)
    assert call["exe"] == str((bindir / "codex").resolve())


def test_find_codex_takes_the_first_real_codex_and_names_the_shim_it_skipped(base, monkeypatch):
    shim_dir = _plant_shim(base)
    bindir = _install_fake(base)
    real = str((bindir / "codex").resolve())
    shim = str(shim_dir / "codex")
    assert codex_exec.find_codex(os.pathsep.join((str(shim_dir), str(bindir)))) == (real, shim)
    assert codex_exec.find_codex(os.pathsep.join((str(bindir), str(shim_dir)))) == (real, None)
    with pytest.raises(SystemExit, match="not a codex binary"):
        codex_exec.find_codex(str(shim_dir))
    # A relative entry names the working directory, which is the PR author's clone.
    monkeypatch.chdir(bindir)
    with pytest.raises(SystemExit, match="not on PATH"):
        codex_exec.find_codex(os.pathsep.join(("", os.curdir)))


def test_files_the_clone_carries_under_output_names_never_pass_for_output(base):
    # No clear before the call, which fails before codex runs: the payload the PR
    # committed must still not survive as this role's output.
    clone = base / "clone"
    clone.mkdir()
    raw = clone / ".pr-review-raw.txt"
    raw.write_text("committed payload")
    Path(f"{raw}.tokens").write_text("999")
    Path(f"{raw}.cost").write_text("0.01")
    Path(f"{raw}.events").mkdir()
    (Path(f"{raw}.events") / "inner").write_text("x")
    result, _ = _run_agent(base, clone=clone, agent="review-agent-missing", clear=False)
    assert result.returncode == 1
    assert _calls(base) == []
    assert raw.read_text() == ""
    assert Path(f"{raw}.events").is_file()
    assert not Path(f"{raw}.tokens").exists()
    assert not Path(f"{raw}.cost").exists()


def test_an_output_name_the_clone_carries_as_a_symlink_redirects_no_write(base):
    victim = base / "victim.txt"
    victim.write_text("operator file\n")
    clone = base / "clone"
    clone.mkdir()
    for suffix in ("", ".stderr", ".events", ".tokens"):
        Path(f"{clone / '.pr-review-raw.txt'}{suffix}").symlink_to(victim)
    result, raw = _run_agent(base, clone=clone, clear=False)
    assert result.returncode == 0, result.stderr
    assert raw.read_text() == RESULT
    assert victim.read_text() == "operator file\n"


def _write_roles(clone: Path, roles: tuple[str, ...]) -> list[str]:
    args = []
    for role in roles:
        (clone / f"{role}-prompt.txt").write_text(f"{role} task\n")
        args += [
            role,
            f"pr1:{role}",
            f"{role}-prompt.txt",
            str(clone / f".pr-review-raw-{role}.txt"),
            f"review-agent-{role}",
        ]
    return args


_FAN_OUT = (
    f"REVIEW_HARNESS=codex; REVIEW_MODEL={MODEL}; REVIEW_EFFORT={EFFORT}; "
    'cd "$1"; shift; rc=0; run_role_agents "$@" || rc=$?; exit "$rc"'
)


def test_role_fan_out_gives_each_role_its_own_raw_file_and_tolerates_a_failed_role(base):
    clone = base / "clone"
    clone.mkdir()
    args = _write_roles(clone, ("code", "intent"))
    result = _bash(
        base,
        _FAN_OUT,
        [str(clone), "30", *args],
        env={"FAKE_CODEX_FAIL_ON": "intent task", "CLAUDE_SLOT_POOL_SIZE": "1"},
    )
    # The failed role is reported, and its sibling's payload still lands.
    assert result.returncode == 1, result.stderr
    assert "intent role exited 1" in result.stderr
    assert (clone / ".pr-review-raw-code.txt").read_text() == "code task\n"
    assert (clone / ".pr-review-raw-intent.txt").read_text() == ""
    systems = {
        c["stdin"].split()[0]: _config(c["argv"])["developer_instructions"].strip()
        for c in _calls(base)
    }
    assert systems == {role: _body(f"review-agent-{role}").strip() for role in ("code", "intent")}
    # Every slot is back in the pool once the fan-out returns.
    assert not list((base / "state").glob("claude-slot-*.lock"))


def test_a_role_past_the_cap_is_named_and_surfaces_as_a_timeout(base):
    clone = base / "clone"
    clone.mkdir()
    args = _write_roles(clone, ("code", "intent"))
    result = _bash(
        base,
        _FAN_OUT,
        [str(clone), "1", *args],
        env={"FAKE_CODEX_HANG_ON": "intent task", **_FAST_TIMEOUT},
    )
    assert result.returncode == TIMEOUT_EXIT, result.stderr
    assert "intent role exceeded 1s" in result.stderr
    assert "code role exceeded" not in result.stderr
    assert (clone / ".pr-review-raw-code.txt").read_text() == "code task\n"


@pytest.mark.parametrize("role", ["code", "intent"])
def test_each_harness_tells_the_role_where_its_payload_goes(role):
    def prompt(harness: str) -> str:
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{LIB}"; print_role_task "$@"',
                "_",
                role,
                harness,
                "diff.txt",
                "intent.md",
                "payload.txt",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    claude, codex = prompt("claude"), prompt("codex")
    # A read-only sandbox cannot write the file the orchestrated role writes.
    assert "payload.txt" in claude and "payload.txt" not in codex
    assert "Your final message is your payload" in codex
    for text in (claude, codex):
        assert "diff.txt" in text
        assert ("intent.md" in text) == (role == "intent")


@pytest.mark.parametrize("where", ["role payloads", "orchestrator transcript"])
@pytest.mark.parametrize(("harness", "pauses"), [("codex", False), ("claude", True)])
def test_session_limit_text_pauses_polling_only_under_claude(tmp_path, where, harness, pauses):
    # A Codex quota hit never writes Claude's sentence, so under codex the text can
    # only be the PR's doing, and one PR must not stop the daemon reviewing others.
    sentence = "You've hit your session limit · resets 5pm"
    transcript = tmp_path / ".pr-review-orchestrator.txt"
    transcript.write_text(sentence if where == "orchestrator transcript" else "")
    raws = [tmp_path / f".pr-review-raw-{role}.txt" for role in ("code", "intent")]
    for raw in raws:
        raw.write_text(sentence if where == "role payloads" else "")
    state = tmp_path / "state"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{LIB}"; rc=0; merge_role_payloads "$@" || rc=$?; '
            'printf "%s\\n" "$MERGE_CATEGORY" "${STATUS_PAUSE_UNTIL:-}"; exit "$rc"',
            "_",
            harness,
            str(transcript),
            str(tmp_path / "author.json"),
            str(tmp_path / "merge.err"),
            *map(str, raws),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PR_REVIEW_STATE_DIR": str(state)},
    )
    assert result.returncode == 1, result.stderr
    category, pause_until = result.stdout.splitlines()
    assert category == ("session-limit" if pauses else "all-lenses-failed")
    assert bool(pause_until) == pauses
    assert (state / "session-pause.epoch").exists() == pauses


# --- a real codex under the generated profile ---


def _installed_codex() -> str | None:
    """The codex the daemon would run, or None when there is none."""
    try:
        return codex_exec.find_codex(os.environ.get("PATH", ""))[0]
    except SystemExit:
        return None


@pytest.mark.skipif(_installed_codex() is None, reason="codex is not installed (CI has none)")
@pytest.mark.skipif(
    sys.platform != "darwin", reason="the profile's temp-root denies name macOS paths"
)
def test_the_profile_confines_real_commands(base):
    codex = _installed_codex()
    clone = base / "clone"
    clone.mkdir()
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    (clone / "in.txt").write_text("workspace\n")
    (base / "sibling.txt").write_text("sibling\n")
    argv = codex_exec.build_argv(codex, str(clone), str(clone / "raw"), MODEL, EFFORT, None)
    config = [x for i, a in enumerate(argv) if a == "-c" for x in ("-c", argv[i + 1])]

    def sh(command: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [codex, "sandbox", *config, "-P", codex_exec.PROFILE, "-C", str(clone)]
            + ["--", "sh", "-c", command],
            capture_output=True,
            text=True,
            cwd=clone,
            env={**os.environ, "PRA_ENV_MARKER": "leaked"},
            timeout=60,
        )

    temp_files = [
        Path(tempfile.mkstemp(dir=root, prefix="pr-review-agent-test-")[1])
        for root in ("/tmp", "/var/tmp")
    ]
    try:
        for path in temp_files:
            path.write_text("temp\n")
            assert sh(f"cat {path}").returncode != 0, f"{path} is readable"
            assert sh(f"ls {path.parent}").stdout == "", f"{path.parent} is listable"
            assert sh(f"touch {path.parent}/pr-review-agent-written").returncode != 0
    finally:
        for path in temp_files:
            path.unlink(missing_ok=True)
    assert sh(f"cat {base / 'sibling.txt'}").returncode != 0
    assert sh("cat in.txt").stdout == "workspace\n"
    assert sh("touch written.txt").returncode != 0
    assert not (clone / "written.txt").exists()
    assert "PRA_ENV_MARKER" not in sh("env").stdout
    # git reads files under the home directory by default, which the profile denies.
    status = sh("git status")
    assert status.returncode == 0, status.stderr
    assert "unable to access" not in status.stderr


# --- REVIEW_HARNESS and REVIEW_MODEL resolution ---


def _resolve(fn: str, dotenv: Path, env: dict) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if k not in ("REVIEW_HARNESS", "REVIEW_MODEL")}
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{LIB}"; {fn}', "_", str(dotenv)],
        capture_output=True,
        text=True,
        env={**clean, **env},
    )


def test_harness_env_wins_over_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("REVIEW_HARNESS=claude\n")
    result = _resolve('resolve_review_harness "$1"', dotenv, {"REVIEW_HARNESS": "codex"})
    assert result.stdout == "codex"


def test_harness_falls_back_to_dotenv_then_defaults_to_claude(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("REVIEW_HARNESS=codex\n")
    assert _resolve('resolve_review_harness "$1"', dotenv, {}).stdout == "codex"
    assert _resolve('resolve_review_harness "$1"', tmp_path / "none", {}).stdout == "claude"


def test_unknown_harness_fails_instead_of_falling_back(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("REVIEW_HARNESS=cluade\n")
    result = _resolve('resolve_review_harness "$1"', dotenv, {})
    assert result.returncode != 0
    assert result.stdout == ""
    assert "REVIEW_HARNESS='cluade'" in result.stderr


def test_harness_template_default_matches_the_code_default(tmp_path):
    lines = [ln for ln in TEMPLATE.read_text().splitlines() if ln.startswith("REVIEW_HARNESS=")]
    assert lines, "REVIEW_HARNESS missing from the .env template"
    code_default = _resolve('resolve_review_harness "$1"', tmp_path / "none", {}).stdout
    assert lines[0].split("=", 1)[1] == code_default


def test_codex_requires_an_explicit_model(tmp_path):
    result = _resolve('resolve_review_model "$1" codex', tmp_path / "none", {})
    assert result.returncode != 0
    assert result.stdout == ""
    assert "REVIEW_MODEL" in result.stderr


def test_codex_refuses_a_claude_model_at_startup(tmp_path):
    # The template ships a Claude model ID, so switching only the harness keeps it.
    dotenv = tmp_path / ".env"
    dotenv.write_text("REVIEW_MODEL=claude-example\n")
    result = _resolve('resolve_review_model "$1" codex', dotenv, {})
    assert result.returncode != 0
    assert result.stdout == ""
    assert "REVIEW_MODEL" in result.stderr
    assert _resolve('resolve_review_model "$1" claude', dotenv, {}).stdout == "claude-example"


def test_codex_takes_the_model_the_operator_set(tmp_path):
    result = _resolve('resolve_review_model "$1" codex', tmp_path / "none", {"REVIEW_MODEL": MODEL})
    assert result.stdout == MODEL


def _run_preflight(
    tmp_path: Path, script: Path, harness: str, *bindirs: Path
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(script), "https://github.com/example/example/pull/1"],
        capture_output=True,
        text=True,
        env={
            "PATH": os.pathsep.join((*map(str, bindirs), "/usr/bin", "/bin")),
            "HOME": str(tmp_path),
            "REVIEW_HARNESS": harness,
            "REVIEW_MODEL": MODEL,
        },
        timeout=30,
    )


def _stub(stubs: Path, *cmds: str) -> None:
    stubs.mkdir(exist_ok=True)
    for cmd in cmds:
        (stubs / cmd).write_text("#!/bin/sh\nexit 0\n")
        (stubs / cmd).chmod(0o755)


@pytest.mark.parametrize("script", [REVIEW_PR, REPLY_PR])
@pytest.mark.parametrize(
    ("harness", "missing", "present"), [("codex", "codex", "claude"), ("claude", "claude", "codex")]
)
def test_preflight_names_and_checks_the_selected_harness_binary(
    tmp_path, script, harness, missing, present
):
    # Stubs for every other preflight command, so the one missing is the harness's.
    stubs = tmp_path / "stubs"
    _stub(stubs, "gh", "jq", "git", "python3", "openssl", "curl", present)
    result = _run_preflight(tmp_path, script, harness, stubs)
    assert result.returncode == 1
    assert f"harness: {harness}" in result.stderr
    assert f"missing '{missing}' on PATH" in result.stderr


@pytest.mark.parametrize("script", [REVIEW_PR, REPLY_PR])
def test_startup_refuses_a_codex_shim_once_with_the_fix(tmp_path, script):
    stubs = tmp_path / "stubs"
    _stub(stubs, "gh", "jq", "git", "openssl", "curl", "manager")
    (stubs / "codex").symlink_to(stubs / "manager")
    (stubs / "python3").symlink_to(sys.executable)
    result = _run_preflight(tmp_path, script, "codex", stubs)
    assert result.returncode == 1
    assert "not a codex binary" in result.stderr
    assert "real codex binary on PATH ahead of the shim" in result.stderr


@pytest.mark.parametrize("script", [REVIEW_PR, REPLY_PR])
def test_startup_names_the_real_codex_it_chose_past_a_shim_once(tmp_path, script):
    stubs = tmp_path / "stubs"
    _stub(stubs, "gh", "jq", "git", "openssl", "curl", "manager")
    (stubs / "codex").symlink_to(stubs / "manager")
    (stubs / "python3").symlink_to(sys.executable)
    real = tmp_path / "real"
    _stub(real, "codex")
    result = _run_preflight(tmp_path, script, "codex", stubs, real)
    # The App key check comes after every harness check, so reaching it means they passed.
    assert "App private key not readable" in result.stderr
    assert result.stderr.count(f"using {(real / 'codex').resolve()}") == 1


@pytest.mark.parametrize("script", [REVIEW_PR, REPLY_PR])
def test_codex_startup_passes_its_harness_checks_without_claude(tmp_path, script):
    stubs = tmp_path / "stubs"
    _stub(stubs, "gh", "jq", "git", "openssl", "curl", "codex")
    (stubs / "python3").symlink_to(sys.executable)
    result = _run_preflight(tmp_path, script, "codex", stubs)
    # The App key check comes after every harness check, so reaching it means they passed.
    assert "App private key not readable" in result.stderr
    assert "claude" not in result.stderr
