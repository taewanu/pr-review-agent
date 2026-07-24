"""Contract tests for the orchestrator dispatch (#299, ADR 0038 amended).

Generation runs as ONE `claude -p` session that spawns the roles as subagents
via the Agent tool. Two cross-file contracts hold it together, and both fail
silently at runtime if broken:

- A subagent's effective tool set is the INTERSECTION of its frontmatter list
  and the parent session's `--tools`. A tool granted in an agent file but
  missing from the orchestrator invocation is simply unavailable to the role,
  with no error: the role would fail to write its payload and the review would
  thin to the surviving roles (the same silent shape as #196's lost lens).
- Each role writes its own fenced payload to its named scratch file; the
  orchestrator never re-emits it. If the write instruction disappears from the
  task prompt, every payload file stays empty and merge_findings.py reads a
  total loss.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_PR = REPO_ROOT / "daemon" / "review-pr.sh"
AGENTS = REPO_ROOT / ".claude" / "agents"


def _role_labels() -> list[str]:
    body = REVIEW_PR.read_text()
    m = re.search(r"(?ms)^LENS_LABELS=\((.*?)\)$", body)
    assert m, "LENS_LABELS array not found in review-pr.sh"
    return m.group(1).split()


def _orchestrator_tools() -> list[str]:
    """The `--tools` list of the orchestrator invocation (the one with Agent)."""
    body = REVIEW_PR.read_text()
    tool_lists = [m.split() for m in re.findall(r"--tools ([A-Za-z ]+?) \\?\n", body)]
    with_agent = [t for t in tool_lists if "Agent" in t]
    assert len(with_agent) == 1, (
        f"expected exactly one --tools list containing Agent, found {len(with_agent)}"
    )
    return with_agent[0]


def _frontmatter_tools(label: str) -> list[str]:
    text = (AGENTS / f"review-agent-{label}.md").read_text()
    m = re.search(r"(?m)^tools: (.+)$", text)
    assert m, f"review-agent-{label}.md has no tools frontmatter"
    return [t.strip() for t in m.group(1).split(",")]


def test_orchestrator_tools_cover_every_role_frontmatter_tool():
    parent = set(_orchestrator_tools())
    for label in _role_labels():
        missing = [t for t in _frontmatter_tools(label) if t not in parent]
        assert not missing, (
            f"review-agent-{label} grants {missing} but the orchestrator's "
            f"--tools omits them; the intersection rule silently strips them"
        )


def test_task_prompt_tells_each_role_to_write_its_payload_file():
    body = REVIEW_PR.read_text()
    # The instruction and the per-role file name must live in the same prompt
    # block: the write target is the payload contract with merge_findings.py.
    assert "fenced payload" in body and "to a new file named %s" in body, (
        "the role task prompt no longer instructs writing the payload to the per-role file"
    )


def test_orchestrator_charges_one_slot_per_role():
    body = REVIEW_PR.read_text()
    assert re.search(r'acquire_claude_slots "\$role_count"', body), (
        "orchestrator must charge role_count slots (ADR 0038 amended, #299)"
    )
    assert "release_claude_slots" in body


def test_orchestrator_loads_agents_from_project_settings():
    body = REVIEW_PR.read_text()
    m = re.search(r"(?ms)--tools Agent.*?--output-format stream-json", body)
    assert m, "orchestrator invocation not found"
    assert "--setting-sources project" in m.group(0), (
        "without --setting-sources project the bundled .claude/agents/ "
        "definitions never load and the Agent tool has no role types to spawn"
    )


def test_orchestrator_auto_approves_payload_writes():
    # Non-interactive claude auto-denies the Write permission prompt, so
    # without acceptEdits every role completes its review and then fails to
    # land its payload: a full-cost run producing an empty review, caught only
    # by the first live smoke run (#299).
    body = REVIEW_PR.read_text()
    m = re.search(r"(?ms)--tools Agent.*?--output-format stream-json", body)
    assert m and "--permission-mode acceptEdits" in m.group(0), (
        "orchestrator invocation must carry --permission-mode acceptEdits"
    )
