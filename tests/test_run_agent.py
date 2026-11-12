"""Tests for daemon/lib.sh's run_agent, the one seam every agent call goes through.

Each daemon call site is resolved from the script itself (its mode and whether it
passes a system prompt), then run against a fake `claude`, so the argv asserted
here is the argv that site sends, not a copy of it. That includes the --model and
--effort pins: an unpinned call reviews on the operator's global model, and the
review still reads like a review, so nothing else would notice.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from agent_fixture import (
    EFFORT,
    MODEL,
    PROMPT,
    RESULT,
    run_agent,
    run_agent_calls,
    system_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_PR = REPO_ROOT / "daemon" / "review-pr.sh"
REPLY_PR = REPO_ROOT / "daemon" / "reply-pr.sh"

TIMEOUT_EXIT = 142

_COMMON_TAIL = [
    "--strict-mcp-config",
    "--setting-sources",
    "project",
    "--disable-slash-commands",
    "--output-format",
    "stream-json",
    "--verbose",
]


def _read_only_argv(system: str) -> list[str]:
    return [
        "-p",
        "--append-system-prompt-file",
        system,
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


_ORCHESTRATE_ARGV = [
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
    *_COMMON_TAIL,
]


def _site(script: Path, label: str) -> list[str]:
    matches = [c for c in run_agent_calls(script) if c[5].endswith(f":{label}${{PR_COLOR_RESET}}")]
    assert len(matches) == 1, f"{script.name}: expected one run_agent call labelled {label}"
    return matches[0]


@pytest.mark.parametrize(
    ("script", "label"),
    [
        (REVIEW_PR, "judge-fix"),
        (REVIEW_PR, "review"),
        (REVIEW_PR, "editor"),
        (REPLY_PR, "reply"),
    ],
)
def test_each_call_site_sends_its_exact_argv(tmp_path, script, label):
    words = _site(script, label)
    mode, has_system = words[1], len(words) == 7
    run = run_agent(tmp_path, mode, system=has_system)
    assert run.rc == 0, run.stderr
    if label == "review":
        # The orchestrator alone carries no system prompt: its roles' prompts
        # come from the bundled agent definitions.
        assert (mode, has_system) == ("orchestrate", False)
        assert run.argv == _ORCHESTRATE_ARGV
    else:
        assert (mode, has_system) == ("read-only", True)
        assert run.argv == _read_only_argv(system_path(tmp_path))
    assert run.stdin == PROMPT
    assert run.raw.read_text() == RESULT
    assert Path(f"{run.raw}.cost").read_text() == "0.25"


def test_every_call_site_goes_through_run_agent():
    # Four agent calls exist; a fifth spelled as a bare claude call would skip
    # the model pin and the flags above, so none may appear outside run_agent.
    assert len(run_agent_calls(REVIEW_PR)) == 3
    assert len(run_agent_calls(REPLY_PR)) == 1
    for script in (REVIEW_PR, REPLY_PR):
        text = re.sub(r"\\\n\s*", " ", script.read_text())
        bare = re.findall(r"(?m)^\s*(?:run_with_timeout\s+\S+\s+)?claude\s.*$", text)
        assert not bare, f"{script.name} calls claude directly: {bare}"


def test_nonzero_agent_exit_surfaces_to_the_caller(tmp_path):
    run = run_agent(tmp_path, "read-only", system=True, env={"FAKE_CLAUDE_RC": "3"})
    assert run.rc == 3
    # The formatter still recorded what the agent printed; callers decide.
    assert run.raw.read_text() == RESULT


def test_timeout_surfaces_as_timeout_exit_with_empty_output(tmp_path):
    run = run_agent(
        tmp_path,
        "read-only",
        system=True,
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
