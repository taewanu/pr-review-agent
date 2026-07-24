"""Tests for the intent role's wiring into review-pr.sh (ADR 0035, ADR 0038).

The role itself is a prompt, so what is checkable here is the plumbing around
it: that the two parallel arrays stay index-aligned on the fixed role set, that
intent is the only role handed the intent file, and that a PR describing
nothing skips it instead of paying for a comparison with nothing on one side.

Array alignment is the one that would fail silently. LENS_LABELS and
LENS_RAW_FILES are matched by index (bash 3.2 has no associative arrays, ADR
0013), so an entry added to one of the two sends one role's output to the
other's raw file.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_PR = REPO_ROOT / "daemon" / "review-pr.sh"
AGENTS = REPO_ROOT / ".claude" / "agents"


def _array(name: str) -> list[str]:
    """The first `NAME=(...)` literal in review-pr.sh, split into elements."""
    body = REVIEW_PR.read_text()
    m = re.search(rf"(?ms)^{name}=\((.*?)\)$", body)
    assert m, f"{name} array not found in review-pr.sh"
    return m.group(1).split()


def test_role_arrays_stay_index_aligned():
    labels = _array("LENS_LABELS")
    raws = _array("LENS_RAW_FILES")
    assert len(labels) == len(raws), (
        f"role arrays diverged: {len(labels)} labels, {len(raws)} raw files"
    )
    # ADR 0038: the set is fixed. A third entry here is a design change that
    # belongs in an ADR before it belongs in this array.
    assert labels == ["code", "intent"]


def test_every_role_label_has_an_agent():
    for label in _array("LENS_LABELS"):
        assert (AGENTS / f"review-agent-{label}.md").is_file(), (
            f"{label} role has no agent definition"
        )


def test_intent_is_the_only_role_given_the_intent_file():
    body = REVIEW_PR.read_text()
    for match in re.finditer(r'INTENT_BASENAME"?\s*$', body, re.M):
        window = body[: match.start()].rsplit("\n", 6)[-6:]
        assert any("intent" in line for line in window), (
            "the intent file is passed outside an intent-only branch"
        )


def _skip_decision(tmp_path: Path, body: str, issue_count: int) -> str:
    """Run review-pr.sh's substantive-intent test against a PR body.

    The condition is inline in the script rather than in a function, so it is
    lifted here by the same shape it has there. A drift between the two shows up
    as this test passing while the daemon behaves differently, which is why the
    assertions below pin the observable consequence (skip or run) rather than the
    expression itself.

    The body goes through a file, not through the script text. Interpolating it
    would put Python's escapes inside bash single quotes, where a `\\n` stays two
    literal characters and a whitespace-only body measures as non-empty.
    """
    body_file = tmp_path / "pr-body.txt"
    body_file.write_text(body)
    strip_comments = (
        'import re,sys; print(len(re.sub(r"<!--.*?-->", "", sys.stdin.read(), flags=re.S).strip()))'
    )
    script = f"""
    intent_body_len="$(python3 -c '{strip_comments}' <{body_file})"
    intent_issue_count={issue_count}
    if [[ "$intent_body_len" -gt 0 || "$intent_issue_count" -gt 0 ]]; then
      echo run
    else
      echo skip
    fi
    """
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_a_described_pr_runs_the_role(tmp_path):
    assert _skip_decision(tmp_path, "Moves the voice rules to the editor.", 0) == "run"


def test_an_undescribed_pr_skips_the_role(tmp_path):
    assert _skip_decision(tmp_path, "", 0) == "skip"
    assert _skip_decision(tmp_path, "   \n\n  ", 0) == "skip"


def test_template_boilerplate_alone_skips_the_role(tmp_path):
    # A PR template's HTML comments are text the author never wrote, so counting
    # them would run the role on every PR of every templated repo and find
    # nothing to compare the diff against.
    boilerplate = "<!-- Describe your change -->\n\n<!-- Tests? -->"
    assert _skip_decision(tmp_path, boilerplate, 0) == "skip"


def test_a_linked_issue_alone_runs_the_role(tmp_path):
    # An empty body still leaves something to check when the PR closes an issue:
    # the issue's own ask against the diff.
    assert _skip_decision(tmp_path, "", 1) == "run"


def test_the_skip_leaves_the_code_role():
    # The skip branch collapses the arrays to the code role alone, never to an
    # empty set: the merge step downstream always has at least one payload.
    body = REVIEW_PR.read_text()
    m = re.search(r"(?ms)^else\n\s*LENS_LABELS=\((.*?)\)\n\s*LENS_RAW_FILES=", body)
    assert m, "the intent-skip branch no longer resets the role arrays"
    assert m.group(1).split() == ["code"]


def test_intent_agent_forbids_the_other_types():
    agent = (AGENTS / "review-agent-intent.md").read_text()
    assert 'type="intent"' in agent, "the intent agent does not pin its type"
    assert 'severity="pre_existing"' in agent, (
        "the intent agent does not forbid pre_existing, which the pipeline drops"
    )


def test_intent_file_carries_the_commit_messages_rung():
    # ADR 0038: commit messages joined the intent ladder for the refactor-claim
    # check. The builder must emit the section even when the fetch came back
    # empty, so the role reads a named gap instead of guessing.
    body = REVIEW_PR.read_text()
    assert "## Commit messages" in body, (
        "build_intent_file no longer writes the commit-messages section"
    )


def test_editor_verifies_intent_findings_against_the_intent_file():
    editor = (AGENTS / "review-agent-editor.md").read_text()
    assert "intent" in editor and ".pr-review-intent.md" in editor, (
        "the editor's drop rule would delete every intent finding (ADR 0035)"
    )
