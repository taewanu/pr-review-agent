"""Tests for daemon/lib.sh's `bundle_operator_agents`.

The function copies the operator's `.claude/agents/review-agent-*.md` and
`.claude/commands/review-pr-*.md` (both globs, so a new lens needs no list
update for either its agent or its dispatch command) plus an explicit list of
the non-lens commands (review-pr.md, edit-review.md, reply-pr.md,
judge-fix.md, which aren't name-prefixed the same way) into a scratch clone so
`claude -p` can load them without target-repo setup (ADR 0007). Target-repo
files (if already present) must win, a repo can customize without forcing a
daemon restart.
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
        assert (scratch / ".claude/agents/review-agent-default.md").exists()
        assert (scratch / ".claude/agents/review-agent-correctness.md").exists()
        assert (scratch / ".claude/agents/review-agent-perf.md").exists()
        assert (scratch / ".claude/agents/review-agent-security.md").exists()
        assert (scratch / ".claude/agents/review-agent-tests.md").exists()
        assert (scratch / ".claude/agents/review-agent-reply.md").exists()
        assert (scratch / ".claude/agents/review-agent-editor.md").exists()
        assert (scratch / ".claude/commands/review-pr.md").exists()
        assert (scratch / ".claude/commands/review-pr-correctness.md").exists()
        assert (scratch / ".claude/commands/review-pr-perf.md").exists()
        assert (scratch / ".claude/commands/review-pr-security.md").exists()
        assert (scratch / ".claude/commands/review-pr-tests.md").exists()
        assert (scratch / ".claude/commands/edit-review.md").exists()
        assert (scratch / ".claude/commands/reply-pr.md").exists()


def test_copied_content_matches_source():
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        rc, _ = _bundle(scratch)
        assert rc == 0
        src = (REPO_ROOT / ".claude/agents/review-agent-default.md").read_text()
        dst = (scratch / ".claude/agents/review-agent-default.md").read_text()
        assert src == dst


def test_target_repo_file_wins_over_operator():
    # Pre-stage a customized review-agent-default in the scratch. Bundle must
    # NOT clobber it; target-repo customization is the whole point of the
    # precedence rule.
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        (scratch / ".claude/agents").mkdir(parents=True)
        custom = "---\nname: review-agent-default\n---\nTARGET CUSTOM\n"
        (scratch / ".claude/agents/review-agent-default.md").write_text(custom)
        rc, _ = _bundle(scratch)
        assert rc == 0
        assert (scratch / ".claude/agents/review-agent-default.md").read_text() == custom
        # Sibling that wasn't pre-staged still gets bundled.
        assert (scratch / ".claude/agents/review-agent-reply.md").exists()


def test_target_repo_command_file_wins_over_operator():
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        (scratch / ".claude/commands").mkdir(parents=True)
        custom = "---\ndescription: custom review\n---\nTARGET CUSTOM\n"
        (scratch / ".claude/commands/review-pr.md").write_text(custom)
        rc, _ = _bundle(scratch)
        assert rc == 0
        assert (scratch / ".claude/commands/review-pr.md").read_text() == custom
        assert (scratch / ".claude/commands/reply-pr.md").exists()


def test_future_lens_command_bundled_by_glob_with_no_code_change():
    # Proves the review-pr-*.md glob (ADR 0023), not just today's known 4 lens
    # names: a hypothetical new lens's command, planted transiently in the real
    # source tree, must be picked up with no change to bundle_operator_agents.
    fake_command = REPO_ROOT / ".claude/commands/review-pr-zzz-test-lens.md"
    assert not fake_command.exists(), "fixture collision: pick a different fake name"
    fake_command.write_text("---\ndescription: fixture only, not a real lens\n---\n")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            rc, _ = _bundle(scratch)
            assert rc == 0
            assert (scratch / ".claude/commands/review-pr-zzz-test-lens.md").exists()
    finally:
        fake_command.unlink()


def test_creates_directories_if_missing():
    # Scratch has nothing relevant. Bundle must create both `.claude/agents`
    # and `.claude/commands`. Sounds-abroad-style scratch (other `.claude/`
    # subdirs present but not `agents/commands`) is the realistic case.
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        (scratch / ".claude/hooks").mkdir(parents=True)
        (scratch / ".claude/settings.json").write_text("{}")
        rc, _ = _bundle(scratch)
        assert rc == 0
        assert (scratch / ".claude/agents").is_dir()
        assert (scratch / ".claude/commands").is_dir()
        # Unrelated existing `.claude/` contents untouched.
        assert (scratch / ".claude/hooks").is_dir()
        assert (scratch / ".claude/settings.json").read_text() == "{}"
