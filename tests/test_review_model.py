"""Tests for daemon/lib.sh's resolve_review_model helper (#209).

The dial exists because the daemon pinned no model and every claude -p inherited
the operator's global ~/.claude/settings.json one, silently reviewing on it. That
regression is invisible from the outside, since a review on the wrong model still
looks like a review, so the resolution order and the default are asserted here
rather than left to a dogfood run to catch.

The helper lives in lib.sh, not inline in review-pr.sh, so a test can source it
without gh, claude, or a clone (the same reason emit_dryrun_contract does; see
test_dry_run.py). The four call sites' `--model "$REVIEW_MODEL"` expansion is
covered by test_model_pinned_at_every_claude_call below, which reads the scripts.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"
REVIEW_PR = REPO_ROOT / "daemon" / "review-pr.sh"
REPLY_PR = REPO_ROOT / "daemon" / "reply-pr.sh"
TEMPLATE = REPO_ROOT / "templates" / ".env.example"

DEFAULT_MODEL = "claude-opus-4-8"


def _resolve(dotenv: Path, env: dict | None = None) -> str:
    # Under the daemon's own flags: review-pr.sh sources lib.sh with
    # `set -euo pipefail`, so an absent dial must resolve to the default rather
    # than abort. A bare shell would mask that regression.
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; source {LIB}; resolve_review_model {dotenv}",
        ],
        capture_output=True,
        text=True,
        env=env
        if env is not None
        else {k: v for k, v in os.environ.items() if k != "REVIEW_MODEL"},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_env_wins_over_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"REVIEW_MODEL={DEFAULT_MODEL}\n")
    env = {**os.environ, "REVIEW_MODEL": "claude-haiku-4-5-20251001"}
    assert _resolve(dotenv, env=env) == "claude-haiku-4-5-20251001"


def test_falls_back_to_dotenv_when_env_unset(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("FOO=bar\nREVIEW_MODEL=claude-sonnet-5\n")
    assert _resolve(dotenv) == "claude-sonnet-5"


def test_defaults_when_absent_everywhere(tmp_path):
    # The operator-facing bug: with no dial anywhere the daemon must still pin a
    # capable model, not fall through to the machine's global default.
    dotenv = tmp_path / ".env"
    dotenv.write_text("FOO=bar\n")
    assert _resolve(dotenv) == DEFAULT_MODEL


def test_defaults_when_dotenv_missing(tmp_path):
    assert _resolve(tmp_path / "nope.env") == DEFAULT_MODEL


def test_empty_dial_defaults_rather_than_emitting_a_broken_flag(tmp_path):
    # `claude --model ""` is a broken invocation, so an empty dial must resolve
    # to the default like an absent one, not pass through.
    dotenv = tmp_path / ".env"
    dotenv.write_text("REVIEW_MODEL=\n")
    assert _resolve(dotenv) == DEFAULT_MODEL


def test_template_default_matches_the_code_default():
    # A template drifted from the code default silently moves every operator who
    # copied it onto a different model (#200/#201).
    keyed = re.search(r"^REVIEW_MODEL=(.+)$", TEMPLATE.read_text(), re.M)
    assert keyed, "REVIEW_MODEL missing from the .env template"
    assert keyed.group(1).strip() == DEFAULT_MODEL


def _claude_invocations(script: Path) -> list[str]:
    """Every `claude -p` invocation in the script, each with its continuation lines.

    Scoped per-invocation rather than matched file-wide: a pattern free to run to
    the next call's flags reports one unpinned site as pinned.
    """
    lines = script.read_text().splitlines()
    invocations = []
    for i, line in enumerate(lines):
        if not re.match(r"^\s*claude -p ", line):
            continue
        block = [line]
        while block[-1].rstrip().endswith("\\") and i + len(block) < len(lines):
            block.append(lines[i + len(block)])
        invocations.append("\n".join(block))
    return invocations


def test_both_scripts_name_the_model_in_the_log():
    # The defect this dial fixes was invisible: a review on the wrong model still
    # reads like a review. Resolving the right value is only half of it; the run
    # has to say which model it used, or the next drift is silent too.
    for script in (REVIEW_PR, REPLY_PR):
        assert 'log_info "model: ${REVIEW_MODEL}"' in script.read_text(), (
            f"{script.name}: resolves REVIEW_MODEL without logging it"
        )


def test_model_pinned_at_every_claude_call():
    # The bug was an unpinned claude -p, so a call site added without --model
    # reintroduces it silently. Every invocation must carry the dial.
    for script in (REVIEW_PR, REPLY_PR):
        invocations = _claude_invocations(script)
        assert invocations, f"{script.name}: no claude -p call found; did the call site move?"
        unpinned = [c for c in invocations if '--model "$REVIEW_MODEL"' not in c]
        assert not unpinned, (
            f"{script.name}: claude -p not pinned to $REVIEW_MODEL:\n" + "\n---\n".join(unpinned)
        )
