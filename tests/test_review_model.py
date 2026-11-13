"""Tests for daemon/lib.sh's resolve_review_model helper (#209).

The dial exists because the daemon pinned no model and every claude -p inherited
the operator's global ~/.claude/settings.json one, silently reviewing on it. That
regression is invisible from the outside, since a review on the wrong model still
looks like a review, so the resolution order and the default are asserted here
rather than left to a dogfood run to catch.

The helper lives in lib.sh, not inline in review-pr.sh, so a test can source it
without gh, claude, or a clone (the same reason emit_dryrun_contract does; see
test_dry_run.py). That every agent call pins the resolved model and effort is
covered by test_run_agent.py, which runs each call site against a fake claude.
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

DEFAULT_MODEL = "claude-opus-5-5"


DEFAULT_EFFORT = "high"


def _run_resolver(
    fn: str, key: str, dotenv: Path, env: dict | None = None
) -> subprocess.CompletedProcess:
    # Under the daemon's own flags: review-pr.sh sources lib.sh with
    # `set -euo pipefail`, so an absent dial must resolve to the default rather
    # than abort. A bare shell would mask that regression.
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail; source {LIB}; {fn} {dotenv}"],
        capture_output=True,
        text=True,
        env=env if env is not None else {k: v for k, v in os.environ.items() if k != key},
    )
    assert result.returncode == 0, result.stderr
    return result


def _resolve(dotenv: Path, env: dict | None = None) -> str:
    return _run_resolver("resolve_review_model", "REVIEW_MODEL", dotenv, env).stdout


def _resolve_effort(dotenv: Path, env: dict | None = None) -> str:
    return _run_resolver("resolve_review_effort", "REVIEW_EFFORT", dotenv, env).stdout


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


def test_template_leaves_the_model_empty_so_a_new_clone_runs_the_setup(tmp_path):
    # An empty value is what makes run.sh offer bin/setup-model.sh, and it must
    # still resolve to the code default for an operator who declines.
    keyed = re.search(r"^REVIEW_MODEL=(.*)$", TEMPLATE.read_text(), re.M)
    assert keyed, "REVIEW_MODEL missing from the .env template"
    assert keyed.group(1) == ""
    assert _resolve(TEMPLATE) == DEFAULT_MODEL


def test_both_scripts_name_the_model_in_the_log():
    # The defect this dial fixes was invisible: a review on the wrong model still
    # reads like a review. Resolving the right value is only half of it; the run
    # has to say which model it used, or the next drift is silent too.
    for script in (REVIEW_PR, REPLY_PR):
        assert 'log_info "model: ${REVIEW_MODEL}"' in script.read_text(), (
            f"{script.name}: resolves REVIEW_MODEL without logging it"
        )


def test_effort_env_wins_over_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("REVIEW_EFFORT=high\n")
    env = {**os.environ, "REVIEW_EFFORT": "max"}
    assert _resolve_effort(dotenv, env=env) == "max"


def test_effort_falls_back_to_dotenv_when_env_unset(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("FOO=bar\nREVIEW_EFFORT=xhigh\n")
    assert _resolve_effort(dotenv) == "xhigh"


def test_effort_defaults_when_absent_everywhere(tmp_path):
    # With no dial the daemon must still pin a level, for the reason
    # resolve_review_effort gives.
    dotenv = tmp_path / ".env"
    dotenv.write_text("FOO=bar\n")
    assert _resolve_effort(dotenv) == DEFAULT_EFFORT


def test_effort_defaults_when_dotenv_missing(tmp_path):
    assert _resolve_effort(tmp_path / "nope.env") == DEFAULT_EFFORT


def test_empty_effort_defaults(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("REVIEW_EFFORT=\n")
    assert _resolve_effort(dotenv) == DEFAULT_EFFORT


def test_unknown_effort_defaults_and_says_so(tmp_path):
    # A typo must fall back loudly, for the reason resolve_review_effort gives.
    dotenv = tmp_path / ".env"
    dotenv.write_text("REVIEW_EFFORT=hihg\n")
    result = _run_resolver("resolve_review_effort", "REVIEW_EFFORT", dotenv)
    assert result.stdout == DEFAULT_EFFORT
    assert "REVIEW_EFFORT='hihg'" in result.stderr


def test_effort_template_default_matches_the_code_default():
    keyed = re.search(r"^REVIEW_EFFORT=(.+)$", TEMPLATE.read_text(), re.M)
    assert keyed, "REVIEW_EFFORT missing from the .env template"
    assert keyed.group(1).strip() == DEFAULT_EFFORT


def test_both_scripts_name_the_effort_in_the_log():
    for script in (REVIEW_PR, REPLY_PR):
        assert 'log_info "effort: ${REVIEW_EFFORT}"' in script.read_text(), (
            f"{script.name}: resolves REVIEW_EFFORT without logging it"
        )
