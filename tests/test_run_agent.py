"""Tests for daemon/lib.sh's run_agent, the one seam every agent call goes through.

Each daemon call site is resolved from the script itself (its mode and the agent it
names), then run against a fake `claude`, so the argv asserted
here is the argv that site sends, not a copy of it. That includes the --model and
--effort pins: an unpinned call reviews on the operator's global model, and the
review still reads like a review, so nothing else would notice.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from agent_fixture import (
    EFFORT,
    MODEL,
    PROMPT,
    RESULT,
    CallSite,
    call_site,
    run_agent,
    run_agent_calls,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS = REPO_ROOT / ".claude" / "agents"
REVIEW_PR = REPO_ROOT / "daemon" / "review-pr.sh"
REPLY_PR = REPO_ROOT / "daemon" / "reply-pr.sh"
LIB = REPO_ROOT / "daemon" / "lib.sh"

TIMEOUT_EXIT = 142

_COMMON_TAIL = [
    "--restricted",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--output-format",
    "stream-json",
    "--verbose",
]


def _read_only_argv(raw: Path) -> list[str]:
    return [
        "-p",
        "--append-system-prompt-file",
        f"{raw}.system.md",
        "--model",
        MODEL,
        "--effort",
        EFFORT,
        "--tools",
        "Read",
        "Grep",
        "Glob",
        "Bash",
        *_COMMON_TAIL,
    ]


def _orchestrate_argv(raw: Path) -> list[str]:
    return [
        "-p",
        "--model",
        MODEL,
        "--effort",
        EFFORT,
        "--tools",
        "Agent",
        "Read",
        "Write",
        "Bash",
        "Grep",
        "Glob",
        "WebFetch",
        "--permission-mode",
        "acceptEdits",
        "--agents",
        f"{raw}.agents.json",
        *_COMMON_TAIL,
    ]


def _site(script: Path, phase: str) -> CallSite:
    matches = [s for s in map(call_site, run_agent_calls(script)) if s.phase == phase]
    assert len(matches) == 1, f"{script.name}: expected one run_agent call for phase {phase}"
    return matches[0]


def _definition(path: Path) -> tuple[dict[str, str], str]:
    """An agent file's frontmatter fields and its body."""
    _, frontmatter, body = path.read_text().split("---\n", 2)
    return dict(re.findall(r"(?m)^(\w+): (.*)$", frontmatter)), body


def _flag_value(argv: list[str], flag: str) -> Path:
    return Path(argv[argv.index(flag) + 1])


_READ_ONLY_SITES = [
    (REVIEW_PR, "judge-fix", "review-agent-fix-check"),
    (REVIEW_PR, "editor", "review-agent-editor"),
    (REPLY_PR, "reply", "review-agent-reply"),
]


@pytest.mark.parametrize(
    ("script", "phase", "mode", "agent", "argv"),
    [
        *(
            (script, phase, "read-only", agent, _read_only_argv)
            for script, phase, agent in _READ_ONLY_SITES
        ),
        # The orchestrator alone names no agent: its roles' prompts come from
        # the definitions handed over with --agents.
        (REVIEW_PR, "review", "orchestrate", None, _orchestrate_argv),
    ],
)
def test_each_call_site_sends_its_exact_argv(tmp_path, script, phase, mode, agent, argv):
    site = _site(script, phase)
    assert (site.mode, site.agent) == (mode, agent)
    run = run_agent(tmp_path, site.mode, agent=site.agent)
    assert run.rc == 0, run.stderr
    rest, _ = run.split_settings()
    assert rest == argv(run.raw)
    assert run.stdin == PROMPT
    assert run.raw.read_text() == RESULT
    assert Path(f"{run.raw}.cost").read_text() == "0.25"


@pytest.mark.parametrize(
    ("mode", "agent"), [("read-only", "review-agent-editor"), ("orchestrate", None)]
)
def test_daemon_asks_for_a_fail_closed_sandbox_bounded_to_the_clone(tmp_path, mode, agent):
    clone = tmp_path / "clone"
    clone.mkdir()
    real = tmp_path.resolve() / "real"
    (real / "state").mkdir(parents=True)
    (real / "app.pem").touch()
    (real / "tmp").mkdir()
    # Every path is entered through a symlink, so each the settings name must come out physical.
    links = tmp_path / "links"
    links.mkdir()
    for name in ("clone", "state", "app.pem", "tmp"):
        (links / name).symlink_to(clone if name == "clone" else real / name)
    run = run_agent(
        tmp_path,
        mode,
        agent=agent,
        cwd=links / "clone",
        env={
            "TMPDIR": str(links / "tmp"),
            "PR_REVIEW_STATE_DIR": str(links / "state"),
            "APP_KEY_PATH": str(links / "app.pem"),
        },
    )
    assert run.rc == 0, run.stderr
    sandbox = run.split_settings()[1]["sandbox"]
    assert sandbox["enabled"] and sandbox["failIfUnavailable"]
    assert not sandbox["allowUnsandboxedCommands"] and not sandbox["autoAllowBashIfSandboxed"]
    assert sandbox["network"] == {"strictAllowlist": True}
    fs = sandbox["filesystem"]
    assert len(fs["denyRead"]) == len(set(fs["denyRead"]))
    assert set(fs["denyRead"]) == {
        "~/",
        str(real / "tmp"),
        _mktemp_dir({"TMPDIR": str(links / "tmp")}),
        str(Path("/tmp").resolve()),
        str(real / "state"),
        str(REPO_ROOT.resolve()),
    }
    # The narrower allowRead wins, which is what keeps the clone readable when
    # it sits under a denied root, as it does under the temp directory.
    agent_tmp = Path(run.env["CLAUDE_CODE_TMPDIR"]).resolve()
    assert fs["allowRead"] == [str(clone.resolve()), str(agent_tmp)]
    assert fs["denyWrite"] == [str(clone.resolve())]
    assert sandbox["credentials"] == {"files": [{"path": str(real / "app.pem"), "mode": "deny"}]}


def _mktemp_dir(env: dict[str, str]) -> str:
    """Where the daemon's own `mktemp -t` files land under `env`."""
    out = subprocess.run(
        ["mktemp", "-u", "-t", "probe.XXXXXX"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **env},
    )
    return str(Path(out.stdout.strip()).parent.resolve())


def _user_temp_dir() -> str:
    if sys.platform == "darwin":
        out = subprocess.run(
            ["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, check=True
        )
        return str(Path(out.stdout.strip()).resolve())
    return str(Path("/tmp").resolve())


def test_an_empty_tmpdir_still_denies_the_user_temp_directory_once(tmp_path):
    run = run_agent(tmp_path, "read-only", agent="review-agent-editor", env={"TMPDIR": ""})
    assert run.rc == 0, run.stderr
    deny = run.split_settings()[1]["sandbox"]["filesystem"]["denyRead"]
    assert _user_temp_dir() in deny
    assert len(deny) == len(set(deny))


@pytest.mark.parametrize("rc", [0, 3])
def test_each_call_gets_its_own_short_temp_directory_removed_afterwards(tmp_path, rc):
    # A long path makes Claude Code hand sandboxed commands its shared temp directory.
    run = run_agent(
        tmp_path, "read-only", agent="review-agent-reply", env={"FAKE_CLAUDE_RC": str(rc)}
    )
    assert run.rc == rc
    agent_tmp = Path(run.env["CLAUDE_CODE_TMPDIR"])
    assert agent_tmp.parent == Path("/tmp")
    assert agent_tmp.name.startswith("pr-review-claude.")
    assert run.tmp_existed
    assert not agent_tmp.exists()


def test_a_terminated_call_removes_its_temp_directory(tmp_path):
    run = run_agent(
        tmp_path,
        "read-only",
        agent="review-agent-reply",
        env={"FAKE_CLAUDE_SLEEP": "30"},
        terminate=True,
    )
    agent_tmp = Path(run.env["CLAUDE_CODE_TMPDIR"])
    assert run.tmp_existed
    assert not agent_tmp.exists()


def _require_claude_version(tmp_path: Path, reported: str) -> subprocess.CompletedProcess:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(f"#!/usr/bin/env bash\necho '{reported}'\n")
    fake.chmod(0o755)
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{LIB}"; require_claude_version'],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"},
    )


@pytest.mark.parametrize("reported", ["2.1.281 (Claude Code)", "2.10.0 (Claude Code)", "3.0.0"])
def test_a_claude_new_enough_for_the_sandbox_settings_passes_the_preflight(tmp_path, reported):
    result = _require_claude_version(tmp_path, reported)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("reported", ["2.1.247 (Claude Code)", "1.9.999", "not a version", ""])
def test_an_older_or_unreadable_claude_fails_the_preflight_with_the_fix(tmp_path, reported):
    result = _require_claude_version(tmp_path, reported)
    assert result.returncode == 1
    assert "claude update" in result.stderr


def test_git_inside_the_agent_skips_its_config_under_the_home_directory(tmp_path):
    run = run_agent(tmp_path, "read-only", agent="review-agent-reply")
    assert run.rc == 0, run.stderr
    git_env = {k: v for k, v in run.env.items() if k.startswith("GIT_CONFIG")}
    assert git_env == {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.excludesFile",
        "GIT_CONFIG_VALUE_0": "/dev/null",
        "GIT_CONFIG_KEY_1": "core.attributesFile",
        "GIT_CONFIG_VALUE_1": "/dev/null",
    }


@pytest.mark.parametrize(("script", "phase", "agent"), _READ_ONLY_SITES)
def test_read_only_system_prompt_is_the_daemon_definitions_body(tmp_path, script, phase, agent):
    # The clone is the PR head, so a definition it carries is the PR author's.
    planted = tmp_path / "clone" / ".claude" / "agents" / f"{agent}.md"
    planted.parent.mkdir(parents=True)
    planted.write_text("---\nname: planted\n---\nPLANTED INSTRUCTIONS\n")
    run = run_agent(tmp_path, "read-only", agent=_site(script, phase).agent)
    assert run.rc == 0, run.stderr
    system = _flag_value(run.argv, "--append-system-prompt-file").read_text()
    assert system.strip() == _definition(AGENTS / f"{agent}.md")[1].strip()


def test_orchestrator_roles_are_the_daemon_definitions(tmp_path):
    # The clone is the PR head, so a definition it carries is the PR author's.
    planted = tmp_path / "clone" / ".claude" / "agents" / "review-agent-code.md"
    planted.parent.mkdir(parents=True)
    planted.write_text("---\nname: review-agent-code\ntools: Read\n---\nPLANTED INSTRUCTIONS\n")
    run = run_agent(tmp_path, "orchestrate")
    assert run.rc == 0, run.stderr
    handed = json.loads(_flag_value(run.argv, "--agents").read_text())
    expected = {}
    for path in sorted(AGENTS.glob("review-agent-*.md")):
        meta, body = _definition(path)
        expected[meta["name"]] = {
            "description": meta["description"],
            "prompt": body.strip(),
            "tools": [t.strip() for t in meta["tools"].split(",")],
        }
    assert {name: {**d, "prompt": d["prompt"].strip()} for name, d in handed.items()} == expected


@pytest.mark.parametrize(
    ("mode", "agent", "flag"),
    [
        ("read-only", "review-agent-editor", "--append-system-prompt-file"),
        ("orchestrate", None, "--agents"),
    ],
)
def test_definition_file_replaces_a_directory_left_at_its_path(tmp_path, mode, agent, flag):
    # Reserved names are cleared once after the checkout, but an earlier agent
    # in the same review can still create one.
    suffix = ".system.md" if mode == "read-only" else ".agents.json"
    run = run_agent(tmp_path, mode, agent=agent, dirs_left=(f".pr-review-raw.txt{suffix}",))
    assert run.rc == 0, run.stderr
    assert _flag_value(run.argv, flag).read_text().strip()


def test_unknown_agent_refuses_without_running_claude(tmp_path):
    run = run_agent(tmp_path, "read-only", agent="review-agent-missing")
    assert run.rc == 1
    assert run.argv is None
    assert "no agent definition" in run.stderr


def test_every_call_site_goes_through_run_agent():
    # A bare claude call would skip the model pin and the flags above.
    for script in (REVIEW_PR, REPLY_PR):
        assert run_agent_calls(script), f"{script.name} has no run_agent call"
        text = re.sub(r"\\\n\s*", " ", script.read_text())
        bare = re.findall(r"(?m)^\s*(?:run_with_timeout\s+\S+\s+)?claude\s.*$", text)
        assert not bare, f"{script.name} calls claude directly: {bare}"


def test_nonzero_agent_exit_surfaces_to_the_caller(tmp_path):
    run = run_agent(tmp_path, "read-only", agent="review-agent-reply", env={"FAKE_CLAUDE_RC": "3"})
    assert run.rc == 3
    # The formatter still recorded what the agent printed; callers decide.
    assert run.raw.read_text() == RESULT


def test_timeout_surfaces_as_timeout_exit_with_empty_output(tmp_path):
    run = run_agent(
        tmp_path,
        "read-only",
        agent="review-agent-reply",
        # The fake must record its stdin before the cap fires, and a loaded runner
        # can delay its start past 1s.
        timeout_secs=3,
        env={
            "FAKE_CLAUDE_SLEEP": "30",
            "TIMEOUT_POLL_INTERVAL": "1",
            "TIMEOUT_KILL_GRACE": "1",
        },
    )
    assert run.rc == TIMEOUT_EXIT
    assert run.stdin == PROMPT
    assert run.raw.read_text() == ""
    assert not Path(f"{run.raw}.cost").exists()
    assert not Path(run.env["CLAUDE_CODE_TMPDIR"]).exists()


def test_unknown_mode_refuses_without_running_claude(tmp_path):
    run = run_agent(tmp_path, "write-anything")
    assert run.rc == 1
    assert run.argv is None
    assert "unknown mode" in run.stderr
