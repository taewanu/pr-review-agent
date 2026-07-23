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


def test_drop_prints_editor_drop_line_to_stderr(capsys):
    # A drop must leave a trace, matching the confidence gate and the merge cap;
    # log_degradation_warnings surfaces the `editor-drop` prefix to the log (#259).
    author = _author("**A.** one", "**B.** two", "**C.** three")
    edits = apply_edits.EditorPayload.model_validate(
        {
            "summary": "s",
            "decisions": [
                {"index": 0, "action": "keep"},
                {"index": 1, "action": "drop"},
                {"index": 2, "action": "drop"},
            ],
        }
    )
    apply_edits.apply_edits(author, edits)
    err = capsys.readouterr().err
    assert "editor-drop: dropped 2 finding(s) at author index(es) [1, 2]" in err


def test_no_drop_prints_nothing(capsys):
    author = _author("**A.** one", "**B.** two")
    edits = apply_edits.EditorPayload.model_validate(
        {
            "summary": "s",
            "decisions": [{"index": 0, "action": "keep"}, {"index": 1, "action": "keep"}],
        }
    )
    apply_edits.apply_edits(author, edits)
    assert "editor-drop" not in capsys.readouterr().err


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


def test_finalize_applies_and_gates_clean(capsys):
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
    # The downgrade warns only on a miss; clean text must stay silent, or a gate
    # that warned on every payload would still pass this suite.
    assert "voice-warning" not in capsys.readouterr().err


def test_finalize_posts_despite_cosmetic_voice_miss_in_rewrite(capsys):
    # A cosmetic voice miss (em dash) must not discard a review that found a real
    # bug; it posts with a stderr warning instead. Style is polished, not gated.
    author = _author("**Vague.** Could break.")
    edits = _fence(
        {
            "summary": "s",
            "decisions": [
                {"index": 0, "action": "rewrite", "body": "**Sharp.** It breaks — here."}
            ],
        }
    )
    final = apply_edits.finalize(author, edits)
    assert final["comments"][0]["body"] == "**Sharp.** It breaks — here."
    assert "voice-warning" in capsys.readouterr().err


def test_finalize_gate_rejects_fidelity_corruption_keeps_failing(capsys):
    # Corruption (HTML-escaped char) malforms what the reader sees, so it stays
    # fail-closed even as cosmetic misses are downgraded to warnings.
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
    assert exc.value.category == "edit-fidelity"


def test_finalize_no_edits_is_identity_and_skips_fidelity():
    # Zero-finding skip path: author body with an escaped entity is NOT re-emitted
    # by an Editor, so fidelity is off here and the author payload passes through.
    author = {"summary": "Looks clean.", "comments": []}
    final = apply_edits.finalize(author, None)
    assert final == author


def test_finalize_no_edits_posts_despite_summary_voice_miss(capsys):
    # Even on the zero-edit path, a cosmetic summary miss warns rather than fails:
    # the review still posts. (No findings here, but the gate must not raise.)
    author = {"summary": "This change is risky.", "comments": []}
    final = apply_edits.finalize(author, None)
    assert final == author
    assert "voice-warning" in capsys.readouterr().err


# --- append_truncation_note (ADR 0023, post-merge cap truncation) -----------


def test_append_truncation_note_zero_is_a_no_op():
    payload = {"summary": "Clean diff.", "comments": []}
    assert apply_edits.append_truncation_note(payload, 0) == payload


def test_append_truncation_note_singular():
    payload = {"summary": "Clean diff.", "comments": []}
    result = apply_edits.append_truncation_note(payload, 1)
    expected = "Clean diff.\n\n1 additional finding omitted by the review cap."
    assert result["summary"] == expected


def test_append_truncation_note_plural():
    payload = {"summary": "Clean diff.", "comments": []}
    result = apply_edits.append_truncation_note(payload, 5)
    expected = "Clean diff.\n\n5 additional findings omitted by the review cap."
    assert result["summary"] == expected


def test_append_truncation_note_makes_no_severity_claim():
    # The dropped tail can be important when a PR exceeds the cap in important
    # findings, so the note must not call the omitted findings low-severity (#194).
    payload = {"summary": "Clean diff.", "comments": []}
    result = apply_edits.append_truncation_note(payload, 12)
    assert "severity" not in result["summary"]


def test_append_truncation_note_does_not_mutate_the_comments():
    payload = {"summary": "s", "comments": [{"path": "a.py", "line": 1}]}
    result = apply_edits.append_truncation_note(payload, 2)
    assert result["comments"] == payload["comments"]


def test_append_truncation_note_skips_the_voice_gate():
    # The note is fixed chrome appended after finalize()'s gate already ran, so
    # a payload that would otherwise fail the gate (e.g. a forbidden opener) is
    # untouched here; this function only appends, it never re-validates.
    payload = {"summary": "This change is risky.", "comments": []}
    result = apply_edits.append_truncation_note(payload, 3)
    assert result["summary"].startswith("This change is risky.")
    assert "3 additional" in result["summary"]


# --- editor bypass note ------------------------------------------------------


def test_append_editor_bypass_note_marks_the_summary():
    payload = {"summary": "Two helpers renamed.", "comments": []}
    result = apply_edits.append_editor_bypass_note(payload)
    assert result["summary"].startswith("Two helpers renamed.\n\n")
    # A reader must be able to tell a bypassed review from a clean one.
    assert "editorial" in result["summary"].lower()


def test_append_editor_bypass_note_is_voice_clean():
    # Appended after the gate, so it is hand-kept em-dash-free like the other note.
    result = apply_edits.append_editor_bypass_note({"summary": "s", "comments": []})
    assert "—" not in result["summary"]


# --- main(): the post-author fallback ----------------------------------------

import subprocess  # noqa: E402


def _miscounting_edits(n: int) -> str:
    """Editor output with one decision too many: covers 0..n instead of 0..n-1.

    This is the #258 failure: a phantom decision shifts the coverage set past the
    draft, and apply_edits rejects the whole batch as edit-coverage.
    """
    decisions = [{"index": i, "action": "keep"} for i in range(n + 1)]
    return _fence({"summary": "reconciled", "decisions": decisions})


def _run_main(tmp_path: Path, author: dict, edits_raw: str | None, *flags: str):
    author_path = tmp_path / "author.json"
    author_path.write_text(json.dumps(author))
    argv = ["python3", str(APPLY_PATH), "--author", str(author_path), *flags]
    if edits_raw is not None:
        edits_path = tmp_path / "edits.txt"
        edits_path.write_text(edits_raw)
        argv += ["--edits", str(edits_path)]
    return subprocess.run(argv, capture_output=True, text=True)


def test_post_author_fallback_posts_the_draft_when_the_editor_miscounts(tmp_path):
    author = _author("**A.** one", "**B.** two")
    result = _run_main(tmp_path, author, _miscounting_edits(2), "--on-editor-error", "post-author")
    assert result.returncode == 0, result.stderr
    posted = json.loads(result.stdout)
    # The author bodies survive unchanged: this is a full bypass, not a partial
    # apply of the miscounted decisions onto the wrong findings.
    assert [c["body"] for c in posted["comments"]] == ["**A.** one", "**B.** two"]
    assert "editorial" in posted["summary"].lower()
    # The internal category is logged to stderr for the operator, never leaked
    # into the summary a PR author reads.
    assert "warning=editor-bypassed category=edit-coverage" in result.stderr
    assert "edit-coverage" not in posted["summary"]


def test_discard_is_the_default_and_still_fails(tmp_path):
    author = _author("**A.** one", "**B.** two")
    result = _run_main(tmp_path, author, _miscounting_edits(2))
    assert result.returncode == 1
    assert "category=edit-coverage" in result.stderr


def test_post_author_fallback_composes_with_the_voice_warning(tmp_path):
    # The fallback re-gates the author draft with fidelity off, so a cosmetic
    # voice miss warns and still posts (the same fail-open the zero-finding skip
    # gets). The bug the editor would have caught was real, so a style slip is no
    # reason to now discard it. Both the voice warning and the bypass note appear.
    author = {
        "summary": "This change is risky.",  # forbidden opener: a voice miss, not a hard fail
        "comments": [{"path": "a.py", "line": 1, "severity": "nit", "type": "polish", "body": "b"}],
    }
    result = _run_main(tmp_path, author, _miscounting_edits(1), "--on-editor-error", "post-author")
    assert result.returncode == 0, result.stderr
    assert "voice-warning" in result.stderr
    assert "warning=editor-bypassed" in result.stderr
    assert "editorial" in json.loads(result.stdout)["summary"].lower()
