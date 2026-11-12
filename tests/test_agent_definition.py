"""Tests for daemon/lib.sh's agent_definition, which reads one agent file.

It reads a deliberately narrow shape instead of YAML, so every file outside that
shape must stop with an error naming the file and line: a definition read
differently from how its author meant it still runs, as a different agent.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "daemon" / "lib.sh"

HEAD = "---\nname: role\ndescription: does a thing\ntools: Read, Grep\n---\n"


def _definition(tmp_path: Path, text: str) -> subprocess.CompletedProcess:
    path = tmp_path / "agent.md"
    path.write_bytes(text.encode())
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{LIB}"; agent_definition "$1"', "_", path],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_supported_shape_reads_each_field(tmp_path):
    result = _definition(tmp_path, HEAD + "Body line.\n")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "role": {"description": "does a thing", "prompt": "Body line.", "tools": ["Read", "Grep"]}
    }


@pytest.mark.parametrize("quote", ['"', "'"])
def test_one_pair_of_surrounding_quotes_is_stripped(tmp_path, quote):
    text = HEAD.replace("does a thing", f"{quote}a: b{quote}") + "Body.\n"
    result = _definition(tmp_path, text)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["role"]["description"] == "a: b"


def test_body_passes_through_intact_including_a_rule_line(tmp_path):
    body = "First.\n\n---\n\nname: not a field\n"
    result = _definition(tmp_path, HEAD + body)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["role"]["prompt"] == body.rstrip("\n")


@pytest.mark.parametrize(
    ("text", "line"),
    [
        pytest.param(
            HEAD.replace("does a thing", ">-") + "  folded\n" + "Body.\n", 3, id="block-scalar"
        ),
        pytest.param(HEAD.replace("does a thing", "|") + "Body.\n", 3, id="literal-scalar"),
        pytest.param(HEAD.replace("Read, Grep", "[Read, Grep]") + "Body.\n", 4, id="flow-list"),
        pytest.param(
            HEAD.replace("tools: Read, Grep", "tools:\n  - Read") + "Body.\n", 4, id="block-list"
        ),
        pytest.param(
            HEAD.replace("does a thing", '"unclosed') + "Body.\n", 3, id="unmatched-quote"
        ),
        pytest.param(HEAD.replace("does a thing", "text # comment") + "Body.\n", 3, id="comment"),
        pytest.param(HEAD.replace("tools: Read, Grep\n", "") + "Body.\n", 4, id="missing-tools"),
        pytest.param(HEAD.replace("Read, Grep", "") + "Body.\n", 4, id="empty-tools"),
        pytest.param(
            HEAD.replace("name: role\n", "name: role\nmodel: opus\n") + "B.\n", 3, id="unknown-key"
        ),
        pytest.param(
            HEAD.replace("name: role\n", "name: role\nname: x\n") + "B.\n", 3, id="duplicate-key"
        ),
        pytest.param(HEAD.replace("\n", "\r\n") + "Body.\r\n", 1, id="crlf"),
        pytest.param(HEAD + "Body.\r\n", 6, id="crlf-in-body"),
        pytest.param("\n" + HEAD + "Body.\n", 1, id="frontmatter-not-at-top"),
        pytest.param(HEAD.rstrip("-\n") + "\nBody.\n", 5, id="frontmatter-unclosed"),
        pytest.param(HEAD + "\n", 6, id="empty-body"),
    ],
)
def test_unsupported_shape_fails_naming_the_file_and_line(tmp_path, text, line):
    result = _definition(tmp_path, text)
    assert result.returncode != 0
    assert result.stdout == ""
    assert f"{tmp_path / 'agent.md'}:{line}:" in result.stderr
