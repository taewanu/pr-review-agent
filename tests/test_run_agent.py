"""Tests for daemon/lib.sh's run_agent, the one seam every agent call goes through.

Each daemon call site is resolved from the script itself (its mode and the agent it
names), then run against a fake `claude`, so the argv asserted
here is the argv that site sends, not a copy of it. That includes the --model and
--effort pins: an unpinned call reviews on the operator's global model, and the
review still reads like a review, so nothing else would notice.
"""

from __future__ import annotations

import json
import re
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


def _site(script: Path, label: str) -> CallSite:
    suffix = f":{label}${{PR_COLOR_RESET}}"
    matches = [s for s in map(call_site, run_agent_calls(script)) if s.label.endswith(suffix)]
    assert len(matches) == 1, f"{script.name}: expected one run_agent call labelled {label}"
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
    ("script", "label", "mode", "agent", "argv"),
    [
        *(
            (script, label, "read-only", agent, _read_only_argv)
            for script, label, agent in _READ_ONLY_SITES
        ),
        # The orchestrator alone names no agent: its roles' prompts come from
        # the definitions handed over with --agents.
        (REVIEW_PR, "review", "orchestrate", None, _orchestrate_argv),
    ],
)
def test_each_call_site_sends_its_exact_argv(tmp_path, script, label, mode, agent, argv):
    site = _site(script, label)
    assert (site.mode, site.agent) == (mode, agent)
    run = run_agent(tmp_path, site.mode, agent=site.agent)
    assert run.rc == 0, run.stderr
    assert run.argv == argv(run.raw)
    assert run.stdin == PROMPT
    assert run.raw.read_text() == RESULT
    assert Path(f"{run.raw}.cost").read_text() == "0.25"


@pytest.mark.parametrize(("script", "label", "agent"), _READ_ONLY_SITES)
def test_read_only_system_prompt_is_the_daemon_definitions_body(tmp_path, script, label, agent):
    # The clone is the PR head, so a definition it carries is the PR author's.
    planted = tmp_path / "clone" / ".claude" / "agents" / f"{agent}.md"
    planted.parent.mkdir(parents=True)
    planted.write_text("---\nname: planted\n---\nPLANTED INSTRUCTIONS\n")
    run = run_agent(tmp_path, "read-only", agent=_site(script, label).agent)
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
        timeout_secs=1,
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


def test_unknown_mode_refuses_without_running_claude(tmp_path):
    run = run_agent(tmp_path, "write-anything")
    assert run.rc == 1
    assert run.argv is None
    assert "unknown mode" in run.stderr
