"""Tests for daemon/lib.sh's `bundle_operator_agents`.

The function copies the operator's `.claude/agents/review-agent-*.md` (a glob,
so a new agent needs no list update) into a scratch clone so the daemon's
directly-prompted dispatch can load them without target-repo setup (ADR 0007).
Agent files only: every dispatch is directly prompted (ADR 0038, #294), so no
slash-command files exist to bundle. Target-repo files (if already present)
must win, a repo can customize without forcing a daemon restart.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"


def _bundle(scratch: Path) -> tuple[int, str]:
    result = subprocess.run(
        ["bash", "-c", f"source {LIB}; bundle_operator_agents {scratch}"],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stderr


def test_copies_operator_agents_into_empty_scratch():
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        rc, _ = _bundle(scratch)
        assert rc == 0
        assert (scratch / ".claude/agents/review-agent-code.md").exists()
        assert (scratch / ".claude/agents/review-agent-intent.md").exists()
        assert (scratch / ".claude/agents/review-agent-reply.md").exists()
        assert (scratch / ".claude/agents/review-agent-editor.md").exists()
        assert (scratch / ".claude/agents/review-agent-fix-check.md").exists()


def test_copied_content_matches_source():
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        rc, _ = _bundle(scratch)
        assert rc == 0
        src = (REPO_ROOT / ".claude/agents/review-agent-code.md").read_text()
        dst = (scratch / ".claude/agents/review-agent-code.md").read_text()
        assert src == dst


def test_target_repo_file_wins_over_operator():
    # Pre-stage a customized review-agent-code in the scratch. Bundle must
    # NOT clobber it; target-repo customization is the whole point of the
    # precedence rule.
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        (scratch / ".claude/agents").mkdir(parents=True)
        custom = "---\nname: review-agent-code\n---\nTARGET CUSTOM\n"
        (scratch / ".claude/agents/review-agent-code.md").write_text(custom)
        rc, _ = _bundle(scratch)
        assert rc == 0
        assert (scratch / ".claude/agents/review-agent-code.md").read_text() == custom
        # Sibling that wasn't pre-staged still gets bundled.
        assert (scratch / ".claude/agents/review-agent-reply.md").exists()


def test_future_agent_bundled_by_glob_with_no_code_change():
    # Proves the review-agent-*.md glob, not just today's known agent names: a
    # hypothetical new agent, planted transiently in the real source tree, must
    # be picked up with no change to bundle_operator_agents.
    fake_agent = REPO_ROOT / ".claude/agents/review-agent-zzz-test.md"
    assert not fake_agent.exists(), "fixture collision: pick a different fake name"
    fake_agent.write_text("---\nname: review-agent-zzz-test\n---\nfixture only\n")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            rc, _ = _bundle(scratch)
            assert rc == 0
            assert (scratch / ".claude/agents/review-agent-zzz-test.md").exists()
    finally:
        fake_agent.unlink()


def test_creates_agents_directory_if_missing():
    # Scratch has nothing relevant. Bundle must create `.claude/agents`.
    # Sounds-abroad-style scratch (other `.claude/` subdirs present but not
    # `agents`) is the realistic case.
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        (scratch / ".claude/hooks").mkdir(parents=True)
        (scratch / ".claude/settings.json").write_text("{}")
        rc, _ = _bundle(scratch)
        assert rc == 0
        assert (scratch / ".claude/agents").is_dir()
        # Unrelated existing `.claude/` contents untouched.
        assert (scratch / ".claude/hooks").is_dir()
        assert (scratch / ".claude/settings.json").read_text() == "{}"
