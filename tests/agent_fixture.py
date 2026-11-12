"""Runs daemon/lib.sh's run_agent against a fake `claude` first on PATH.

The fake records the argv and stdin it received, then prints a one-event
stream-json result, so a test reads what reached the binary and what the caller
was handed back without a real agent.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"

MODEL = "fixture-model"
EFFORT = "fixture-effort"
PROMPT = "the prompt body\nsecond line\n"
RESULT = "the agent's final text"

_FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys, time
with open(os.environ["FAKE_CLAUDE_LOG"], "w") as f:
    json.dump({"argv": sys.argv[1:], "stdin": sys.stdin.read()}, f)
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
print(json.dumps({"type": "result", "result": os.environ["FAKE_CLAUDE_RESULT"],
                  "total_cost_usd": 0.25}), flush=True)
sys.exit(int(os.environ.get("FAKE_CLAUDE_RC", "0")))
"""


@dataclass
class AgentRun:
    rc: int
    argv: list[str] | None
    stdin: str | None
    raw: Path
    stderr: str


def run_agent(
    tmp_path: Path,
    mode: str,
    *,
    agent: str | None = None,
    timeout_secs: int = 30,
    env: dict[str, str] | None = None,
) -> AgentRun:
    """Call run_agent the way the daemon's call sites do, from inside a clone
    whose reserved names were cleared after the checkout."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(_FAKE_CLAUDE)
    fake.chmod(0o755)
    clone = tmp_path / "clone"
    clone.mkdir(exist_ok=True)
    prompt = clone / "prompt.txt"
    prompt.write_text(PROMPT)
    raw = clone / ".pr-review-raw.txt"
    log = tmp_path / "claude-call.json"

    args = [mode, str(timeout_secs), str(prompt), str(raw), "pr1:label"]
    if agent:
        args.append(agent)
    # The daemon's own flags: pipefail is what carries claude's status through
    # the formatter pipe, so a bare shell would hide that contract.
    script = (
        f'set -euo pipefail; source "{LIB}"; '
        f"REVIEW_MODEL={MODEL}; REVIEW_EFFORT={EFFORT}; "
        f'clear_reserved_names "{clone}"; cd "{clone}"; rc=0; run_agent "$@" || rc=$?; exit "$rc"'
    )
    result = subprocess.run(
        ["bash", "-c", script, "_", *args],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_CLAUDE_LOG": str(log),
            "FAKE_CLAUDE_RESULT": RESULT,
            **(env or {}),
        },
        # A broken kill path would block in `wait` forever.
        timeout=60,
    )
    call = json.loads(log.read_text()) if log.exists() else {}
    return AgentRun(
        rc=result.returncode,
        argv=call.get("argv"),
        stdin=call.get("stdin"),
        raw=raw,
        stderr=result.stderr,
    )


def run_agent_calls(script: Path) -> list[list[str]]:
    """Every run_agent call in the script, split into words, continuations joined."""
    text = re.sub(r"\\\n\s*", " ", script.read_text())
    calls = []
    for line in text.splitlines():
        if re.match(r"^\s*run_agent ", line):
            calls.append(shlex.split(line.split("||")[0]))
    return calls


@dataclass
class CallSite:
    mode: str
    label: str
    agent: str | None


def call_site(words: list[str]) -> CallSite:
    """Read a run_agent call positionally: mode, timeout, prompt, raw, label, [agent name]."""
    return CallSite(mode=words[1], label=words[5], agent=words[6] if len(words) == 7 else None)
