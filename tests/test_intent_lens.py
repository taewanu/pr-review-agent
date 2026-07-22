"""Tests for the intent lens's wiring into review-pr.sh (ADR 0035).

The lens itself is a prompt, so what is checkable here is the plumbing around
it: that its three parallel-array entries stay index-aligned, that it is the only
lens handed the intent file, and that a PR describing nothing skips it instead of
paying for a lens with nothing to compare against.

Array alignment is the one that would fail silently. LENS_COMMANDS, LENS_LABELS
and LENS_RAW_FILES are matched by index (bash 3.2 has no associative arrays, ADR
0013), so a lens added to two of the three sends one lens's output to another
lens's raw file.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_PR = REPO_ROOT / "daemon" / "review-pr.sh"
AGENTS = REPO_ROOT / ".claude" / "agents"
COMMANDS = REPO_ROOT / ".claude" / "commands"


def _array(name: str) -> list[str]:
    """The first `NAME=(...)` literal in review-pr.sh, split into elements."""
    body = REVIEW_PR.read_text()
    m = re.search(rf"(?ms)^{name}=\((.*?)\)$", body)
    assert m, f"{name} array not found in review-pr.sh"
    return m.group(1).split()


def test_lens_arrays_stay_index_aligned():
    commands = _array("LENS_COMMANDS")
    labels = _array("LENS_LABELS")
    raws = _array("LENS_RAW_FILES")
    assert len(commands) == len(labels) == len(raws), (
        f"lens arrays diverged: {len(commands)} commands, "
        f"{len(labels)} labels, {len(raws)} raw files"
    )
    for cmd, label in zip(commands, labels, strict=True):
        # The default lens is `/review-pr`; every other command carries its label.
        if label != "default":
            assert cmd.endswith(f"-{label}"), f"{cmd} is not the {label} lens's command"


def test_every_lens_label_has_an_agent_and_a_command():
    for label in _array("LENS_LABELS"):
        assert (AGENTS / f"review-agent-{label}.md").is_file(), (
            f"{label} lens has no agent definition"
        )
        command = "review-pr.md" if label == "default" else f"review-pr-{label}.md"
        assert (COMMANDS / command).is_file(), f"{label} lens has no slash command"


def test_intent_is_the_only_lens_given_the_intent_file():
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


def test_a_described_pr_runs_the_lens(tmp_path):
    assert _skip_decision(tmp_path, "Moves the voice rules to the editor.", 0) == "run"


def test_an_undescribed_pr_skips_the_lens(tmp_path):
    assert _skip_decision(tmp_path, "", 0) == "skip"
    assert _skip_decision(tmp_path, "   \n\n  ", 0) == "skip"


def test_template_boilerplate_alone_skips_the_lens(tmp_path):
    # A PR template's HTML comments are text the author never wrote, so counting
    # them would run the lens on every PR of every templated repo and find
    # nothing to compare the diff against.
    boilerplate = "<!-- Describe your change -->\n\n<!-- Tests? -->"
    assert _skip_decision(tmp_path, boilerplate, 0) == "skip"


def test_a_linked_issue_alone_runs_the_lens(tmp_path):
    # An empty body still leaves something to check when the PR closes an issue:
    # the issue's own ask against the diff.
    assert _skip_decision(tmp_path, "", 1) == "run"


def _intent_branch(active_labels: list[str], body_len: int, issue_count: int) -> str:
    """Which of the three intent branches review-pr.sh takes, run as bash.

    Lifted rather than sourced: review-pr.sh executes top to bottom against a
    live PR, so it cannot be sourced for one decision. The shape is kept
    identical to the script's, and the tests below pin the outcome (build, skip,
    or neither) rather than the expression, so a rewrite that preserves behaviour
    does not fail here.
    """
    labels = " ".join(active_labels)
    script = f"""
    LENS_LABELS=({labels})
    intent_active=0
    for _label in "${{LENS_LABELS[@]}}"; do
      if [[ "$_label" == "intent" ]]; then
        intent_active=1
      fi
    done
    intent_body_len={body_len}
    intent_issue_count={issue_count}
    intent_only=0
    if [[ "${{#LENS_LABELS[@]}}" -eq 1 && "$intent_active" -eq 1 ]]; then
      intent_only=1
    fi
    if [[ "$intent_active" -eq 0 ]]; then
      echo inactive
    elif [[ "$intent_body_len" -gt 0 || "$intent_issue_count" -gt 0 || "$intent_only" -eq 1 ]]; then
      echo build
    else
      echo drop
    fi
    """
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_the_skip_never_empties_the_lens_set():
    # REVIEW_LENSES=intent plus an undescribed PR would otherwise drop the only
    # lens, leaving the merge step with no payload to read.
    assert _intent_branch(["intent"], body_len=0, issue_count=0) == "build"


def test_a_disabled_intent_lens_builds_nothing():
    # Building the file costs a `gh issue view` per closing reference. Under a
    # REVIEW_LENSES that excludes the lens, nothing would ever read the result,
    # and a per-PR network call is hang surface whether or not it is used.
    assert _intent_branch(["default"], body_len=500, issue_count=7) == "inactive"


def test_an_undescribed_pr_drops_the_lens_when_others_remain():
    assert _intent_branch(["default", "intent"], body_len=0, issue_count=0) == "drop"
    assert _intent_branch(["default", "intent"], body_len=1, issue_count=0) == "build"


def test_intent_agent_forbids_the_other_types():
    agent = (AGENTS / "review-agent-intent.md").read_text()
    assert 'type="intent"' in agent, "the intent agent does not pin its type"
    assert 'severity="pre_existing"' in agent, (
        "the intent agent does not forbid pre_existing, which the pipeline drops"
    )


def test_editor_verifies_intent_findings_against_the_intent_file():
    editor = (AGENTS / "review-agent-editor.md").read_text()
    assert "intent" in editor and ".pr-review-intent.md" in editor, (
        "the editor's drop rule would delete every intent finding (ADR 0035)"
    )
