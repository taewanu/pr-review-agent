"""Tests for lib.sh's `warn_env_drift` (#201).

A tunable added to templates/.env.example plus a code fallback never reaches
an operator .env created before the knob existed, so it silently runs on the
code default. The concrete failure: CLAUDE_SLOT_POOL_SIZE (ADR 0023) missing
from a live .env pinned lens concurrency at 3 instead of the recommended 10,
surfacing only as 7-9 minute reviews dominated by slot waits. The boot-time
warning is the signal that was missing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"


def _warn(tmp_path: Path, env_body: str | None, template_body: str) -> tuple[str, int]:
    env_file = tmp_path / ".env"
    if env_body is not None:
        env_file.write_text(env_body)
    template = tmp_path / ".env.example"
    template.write_text(template_body)
    result = subprocess.run(
        ["bash", "-c", f"source {LIB}; warn_env_drift {env_file} {template}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stderr, result.returncode


def test_missing_template_key_is_warned_by_name(tmp_path):
    err, rc = _warn(
        tmp_path,
        "REPOS=example/example\n",
        "REPOS=\nCLAUDE_SLOT_POOL_SIZE=10\n",
    )
    assert rc == 0
    assert "CLAUDE_SLOT_POOL_SIZE" in err
    assert "REPOS" not in err.replace("CLAUDE_SLOT_POOL_SIZE", "")


def test_fully_synced_env_is_silent(tmp_path):
    err, rc = _warn(
        tmp_path,
        "REPOS=example/example\nMAX_PARALLEL=3\n",
        "REPOS=\nMAX_PARALLEL=3\n",
    )
    assert rc == 0
    assert err == ""


def test_extra_env_keys_beyond_template_are_fine(tmp_path):
    err, rc = _warn(tmp_path, "REPOS=x\nCUSTOM_THING=1\n", "REPOS=\n")
    assert rc == 0
    assert err == ""


def test_commented_out_env_key_counts_as_absent(tmp_path):
    # Commenting a knob out means running on the code default too; the boot
    # line should say so rather than treating the comment as a live value.
    err, rc = _warn(tmp_path, "REPOS=x\n# MAX_PARALLEL=3\n", "REPOS=\nMAX_PARALLEL=3\n")
    assert rc == 0
    assert "MAX_PARALLEL" in err


def test_template_comments_are_not_treated_as_keys(tmp_path):
    err, rc = _warn(tmp_path, "REPOS=x\n", "REPOS=\n# FUTURE_KNOB=1\n")
    assert rc == 0
    assert err == ""


def test_missing_files_are_a_noop(tmp_path):
    # Read-only, non-blocking by contract: a tarball install without the
    # template (or a bad path) must never break boot.
    err, rc = _warn(tmp_path, None, "REPOS=\n")
    assert rc == 0
    assert err == ""
