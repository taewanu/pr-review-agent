"""Every lens prompt must spell out the taxonomy it is required to emit.

An agent's body is its entire system prompt (ADR 0038's direct dispatch):
review-pr.sh strips the yaml header and appends the rest, with no includes and
no file resolution. So "identical to `review-agent-code`" and "per ADR 0002"
both resolve to nothing at runtime, and a role carrying only those pointers has
never been shown its legal values.

Four lenses carried exactly that. Dogfooding sounds-abroad#294 caught the
correctness lens emitting `type: "correctness"`, its own label, on all four of
its findings; per-finding schema validation (ADR 0025) then dropped every one
into a sidecar error file while the review completed normally. The failure mode
is a review that silently thins, which is why it survived until a run happened to
be inspected finding by finding.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_PR = REPO_ROOT / "daemon" / "review-pr.sh"
AGENTS = REPO_ROOT / ".claude" / "agents"

# ADR 0002. Kept as literals rather than imported from extract_json.py: the point
# is that the prompt and the schema agree, so reading both from one source would
# defeat the test.
SEVERITIES = ("important", "nit", "pre_existing")
TYPES = ("bug", "refactor", "polish", "intent")


def _lens_labels() -> list[str]:
    body = REVIEW_PR.read_text()
    m = re.search(r"(?ms)^LENS_LABELS=\((.*?)\)$", body)
    assert m, "LENS_LABELS array not found in review-pr.sh"
    labels = m.group(1).split()
    assert labels, "LENS_LABELS is empty"
    return labels


def test_every_lens_prompt_spells_out_every_severity():
    for label in _lens_labels():
        prompt = (AGENTS / f"review-agent-{label}.md").read_text()
        missing = [v for v in SEVERITIES if f"`{v}`" not in prompt]
        assert not missing, f"{label} lens never spells out severity {missing}"


def test_every_lens_prompt_spells_out_every_type():
    for label in _lens_labels():
        prompt = (AGENTS / f"review-agent-{label}.md").read_text()
        missing = [v for v in TYPES if f"`{v}`" not in prompt]
        assert not missing, f"{label} lens never spells out type {missing}"


def test_no_lens_prompt_defers_its_contract_to_another_prompt():
    # A pointer at another agent's prompt is the shape that failed. Naming a
    # sibling lens is fine; deferring the output contract to one is not.
    for label in _lens_labels():
        prompt = (AGENTS / f"review-agent-{label}.md").read_text()
        assert "contract is identical to `review-agent-code`" not in prompt, (
            f"{label} lens defers its output contract to a prompt it cannot read"
        )
