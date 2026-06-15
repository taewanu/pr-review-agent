"""Tests for daemon/apply_edits.py — apply Editor decisions, gate, emit (#133)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

APPLY_PATH = Path(__file__).resolve().parent.parent / "daemon" / "apply_edits.py"
_spec = importlib.util.spec_from_file_location("apply_edits", APPLY_PATH)
assert _spec is not None and _spec.loader is not None
apply_edits = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(apply_edits)

ApplyError = apply_edits.ApplyError


def _author(*bodies: str) -> dict:
    return {
        "summary": "Two helpers renamed.",
        "comments": [
            {"path": f"f{i}.py", "line": i + 1, "severity": "nit", "type": "polish", "body": b}
            for i, b in enumerate(bodies)
        ],
    }


def _fence(obj: dict) -> str:
    return f"thinking out loud\n\n```json\n{json.dumps(obj)}\n```\n"


# --- apply_edits: the three levers -------------------------------------------


def test_keep_carries_author_body_by_reference():
    author = _author("**Keep me.** Unchanged body with a < bracket.")
    edits = apply_edits.EditorPayload.model_validate(
        {"summary": "One finding stands.", "decisions": [{"index": 0, "action": "keep"}]}
    )
    final = apply_edits.apply_edits(author, edits)
    assert final["comments"][0]["body"] == "**Keep me.** Unchanged body with a < bracket."
    assert final["summary"] == "One finding stands."


def test_keep_ignores_any_body_the_editor_sends():
    author = _author("**Original.** Author wrote this.")
    edits = apply_edits.EditorPayload.model_validate(
        {
            "summary": "s",
            "decisions": [{"index": 0, "action": "keep", "body": "**Stray.** Ignore."}],
        }
    )
    final = apply_edits.apply_edits(author, edits)
    assert final["comments"][0]["body"] == "**Original.** Author wrote this."


def test_rewrite_swaps_body_keeps_other_fields():
    author = _author("**Vague.** Could break.")
    edits = apply_edits.EditorPayload.model_validate(
        {
            "summary": "s",
            "decisions": [
                {"index": 0, "action": "rewrite", "body": "**Sharp.** It throws on null."}
            ],
        }
    )
    final = apply_edits.apply_edits(author, edits)
    c = final["comments"][0]
    assert c["body"] == "**Sharp.** It throws on null."
    assert c["path"] == "f0.py" and c["line"] == 1 and c["severity"] == "nit"


def test_quote_rides_through_keep_and_rewrite_untouched():
    # ADR 0018 boundary: the Editor names a decision by index and never touches
    # `quote`, so it survives both keep and rewrite via the by-reference contract.
    author = {
        "summary": "s",
        "comments": [
            {
                "path": "a.py",
                "line": 1,
                "severity": "nit",
                "type": "polish",
                "body": "**Keep.**",
                "quote": "    keep_me = 1",
            },
            {
                "path": "b.py",
                "line": 2,
                "severity": "nit",
                "type": "polish",
                "body": "**Rewrite.**",
                "quote": "    rewrite_me = 2",
            },
        ],
    }
    edits = apply_edits.EditorPayload.model_validate(
        {
            "summary": "s",
            "decisions": [
                {"index": 0, "action": "keep"},
                {"index": 1, "action": "rewrite", "body": "**Sharper.** Now clear."},
            ],
        }
    )
    final = apply_edits.apply_edits(author, edits)
    assert final["comments"][0]["quote"] == "    keep_me = 1"
    assert final["comments"][1]["quote"] == "    rewrite_me = 2"
    assert final["comments"][1]["body"] == "**Sharper.** Now clear."


def test_drop_omits_and_survivors_keep_order():
    author = _author("**A.** one", "**B.** two", "**C.** three")
    edits = apply_edits.EditorPayload.model_validate(
        {
            "summary": "s",
            "decisions": [
                {"index": 0, "action": "keep"},
                {"index": 1, "action": "drop"},
                {"index": 2, "action": "rewrite", "body": "**C2.** sharper"},
            ],
        }
    )
    final = apply_edits.apply_edits(author, edits)
    assert [c["body"] for c in final["comments"]] == ["**A.** one", "**C2.** sharper"]


# --- coverage and schema -----------------------------------------------------


def test_missing_index_is_a_coverage_error():
    author = _author("**A.** one", "**B.** two")
    edits = apply_edits.EditorPayload.model_validate(
        {"summary": "s", "decisions": [{"index": 0, "action": "keep"}]}
    )
    with pytest.raises(ApplyError) as exc:
        apply_edits.apply_edits(author, edits)
    assert exc.value.category == "edit-coverage"


def test_duplicate_index_is_a_coverage_error():
    author = _author("**A.** one")
    edits = apply_edits.EditorPayload.model_validate(
        {
            "summary": "s",
            "decisions": [{"index": 0, "action": "keep"}, {"index": 0, "action": "drop"}],
        }
    )
    with pytest.raises(ApplyError) as exc:
        apply_edits.apply_edits(author, edits)
    assert exc.value.category == "edit-coverage"


def test_out_of_range_index_is_a_coverage_error():
    author = _author("**A.** one")
    edits = apply_edits.EditorPayload.model_validate(
        {"summary": "s", "decisions": [{"index": 5, "action": "keep"}]}
    )
    with pytest.raises(ApplyError) as exc:
        apply_edits.apply_edits(author, edits)
    assert exc.value.category == "edit-coverage"


def test_rewrite_without_body_is_schema_invalid():
    with pytest.raises(ApplyError) as exc:
        apply_edits._parse_edits(
            _fence({"summary": "s", "decisions": [{"index": 0, "action": "rewrite"}]})
        )
    assert exc.value.category == "edit-schema-invalid"


def test_no_fence_is_edit_no_fence():
    with pytest.raises(ApplyError) as exc:
        apply_edits._parse_edits("just prose, no fence")
    assert exc.value.category == "edit-no-fence"


def test_bad_json_is_edit_parse_error():
    with pytest.raises(ApplyError) as exc:
        apply_edits._parse_edits("```json\n{not valid}\n```")
    assert exc.value.category == "edit-parse-error"


# --- finalize: gate ----------------------------------------------------------


def test_finalize_applies_and_gates_clean():
    author = _author("**Vague.** Could break.")
    edits = _fence(
        {
            "summary": "One finding stands.",
            "decisions": [
                {"index": 0, "action": "rewrite", "body": "**Sharp.** It throws on null."}
            ],
        }
    )
    final = apply_edits.finalize(author, edits)
    assert final["comments"][0]["body"] == "**Sharp.** It throws on null."


def test_finalize_gate_rejects_em_dash_rewrite():
    author = _author("**Vague.** Could break.")
    edits = _fence(
        {
            "summary": "s",
            "decisions": [
                {"index": 0, "action": "rewrite", "body": "**Sharp.** It breaks — here."}
            ],
        }
    )
    with pytest.raises(ApplyError) as exc:
        apply_edits.finalize(author, edits)
    assert exc.value.category == "style-violation"


def test_finalize_gate_rejects_fidelity_corruption_in_rewrite():
    author = _author("**Vague.** Could break.")
    edits = _fence(
        {
            "summary": "s",
            "decisions": [
                {"index": 0, "action": "rewrite", "body": "**Cover `a &lt;= b`.** Untested."}
            ],
        }
    )
    with pytest.raises(ApplyError) as exc:
        apply_edits.finalize(author, edits)
    assert exc.value.category == "style-violation"


def test_finalize_no_edits_is_identity_and_skips_fidelity():
    # Zero-finding skip path: author body with an escaped entity is NOT re-emitted
    # by an Editor, so fidelity is off here and the author payload passes through.
    author = {"summary": "Looks clean.", "comments": []}
    final = apply_edits.finalize(author, None)
    assert final == author


def test_finalize_no_edits_still_gates_summary_voice():
    author = {"summary": "This change is risky.", "comments": []}
    with pytest.raises(ApplyError) as exc:
        apply_edits.finalize(author, None)
    assert exc.value.category == "style-violation"
