"""Tests for the reserved `.pr-review-*` prefix in the per-PR clone.

The clone is the PR head, so its author can commit an entry under any name the
daemon later writes or reads there. Each test plants such an entry in a real
directory and runs the daemon's own code over it: run_agent through a fake
`claude`, and the review-pr.sh fragments lifted verbatim from the script.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from agent_fixture import RESULT, run_agent

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"
MERGE = REPO_ROOT / "daemon" / "merge_findings.py"
REVIEW_PR = REPO_ROOT / "daemon" / "review-pr.sh"
REPLY_PR = REPO_ROOT / "daemon" / "reply-pr.sh"

PREFIX = ".pr-review-"


def _lift(pattern: str) -> str:
    """A fragment of review-pr.sh, so the test runs the code the daemon runs."""
    matches = re.findall(pattern, REVIEW_PR.read_text(), flags=re.M | re.S)
    assert len(matches) == 1, f"expected one match for {pattern!r} in review-pr.sh"
    return matches[0]


def _bash(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{LIB}"; {script}', "_", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _lens_payload(body: str, line: int) -> str:
    finding = {
        "path": "src/main.py",
        "line": line,
        "severity": "important",
        "type": "bug",
        "body": body,
        "confidence": 95,
    }
    return f"```json\n{json.dumps({'summary': 'lens summary', 'comments': [finding]})}\n```\n"


def test_a_planted_symlink_is_not_written_through(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("victim")
    victim_dir = tmp_path / "victim-dir"
    victim_dir.mkdir()
    (victim_dir / "kept.txt").write_text("victim")
    clone = tmp_path / "clone"
    clone.mkdir()
    for name in (".pr-review-raw.txt", ".pr-review-raw.txt.cost", ".pr-review-raw.txt.system.md"):
        (clone / name).symlink_to(victim)
    # A case-insensitive filesystem resolves this to the daemon's stderr sidecar.
    (clone / ".PR-REVIEW-raw.txt.stderr").symlink_to(victim)
    (clone / ".pr-review-dir").symlink_to(victim_dir, target_is_directory=True)

    run = run_agent(tmp_path, "read-only", agent="review-agent-reply")

    assert run.rc == 0, run.stderr
    assert victim.read_text() == "victim"
    assert (victim_dir / "kept.txt").read_text() == "victim"
    assert run.raw.read_text() == RESULT


def test_a_committed_role_payload_is_not_merged(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / ".pr-review-raw-intent.txt").write_text(_lens_payload("PLANTED finding.", 7))
    ensure_payloads = _lift(r"^role_i=0\nwhile .*?^done$")
    # The code role lands its payload and the intent role writes nothing.
    script = f"""
    clear_reserved_names "$1"
    cd "$1"
    printf '%s' "$2" >.pr-review-raw.txt
    LENS_LABELS=(code intent)
    LENS_RAW_FILES=(.pr-review-raw.txt .pr-review-raw-intent.txt)
    role_count=2
    {ensure_payloads}
    python3 "{MERGE}" --no-style "${{LENS_RAW_FILES[@]}}"
    """
    result = _bash(script, str(clone), _lens_payload("Real finding.", 42))

    assert result.returncode == 0, result.stderr
    bodies = [c["body"] for c in json.loads(result.stdout)["comments"]]
    assert bodies == ["Real finding."]


def test_a_committed_cost_file_is_not_summed(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / ".pr-review-planted.txt.cost").write_text("1e400")
    (clone / "planted.cost").write_text("1e400")
    run = run_agent(tmp_path, "read-only", agent="review-agent-reply")
    assert run.rc == 0, run.stderr

    log_cost = _lift(r"^log_total_review_cost\(\) \{\n.*?^\}$")
    result = _bash(f'{log_cost}\nSCRATCH="$1"; DRY_RUN=1; log_total_review_cost', str(clone))

    assert result.returncode == 0, result.stderr
    assert "total review cost: $0.250" in result.stderr


@pytest.mark.parametrize("name", [".pr-review-raw.txt", ".pr-review-raw.txt.stderr"])
def test_a_committed_directory_does_not_fail_the_write(tmp_path, name):
    planted = tmp_path / "clone" / name
    planted.mkdir(parents=True)
    (planted / "inner.txt").write_text("planted")

    run = run_agent(tmp_path, "read-only", agent="review-agent-reply")

    assert run.rc == 0, run.stderr
    assert run.raw.read_text() == RESULT


@pytest.mark.parametrize("script", [REVIEW_PR, REPLY_PR])
def test_each_script_clears_the_prefix_before_its_first_write(script):
    # Nothing but git writes into the clone before this line, so it is the one
    # point where every reserved name is still the PR's.
    assert re.search(
        r'git checkout --quiet --detach "\$HEAD_OID"\n\)\nclear_reserved_names "\$SCRATCH"\n',
        script.read_text(),
    ), f"{script.name}: clear_reserved_names must directly follow the checkout"


@pytest.mark.parametrize("script", [REVIEW_PR, REPLY_PR])
def test_every_clone_path_is_under_the_reserved_prefix(script):
    text = script.read_text()
    names = re.findall(r'\$SCRATCH/([^"\s)]+)', text)
    names += re.findall(r'^\s*\w+_BASENAME="([^"]+)"', text, flags=re.M)
    stray = [n for n in names if not (n.startswith(PREFIX) or re.fullmatch(r"\$\w+_BASENAME", n))]
    assert names and not stray, f"{script.name} writes outside {PREFIX}*: {stray}"
