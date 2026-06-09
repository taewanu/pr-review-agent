"""Tests for daemon/extract-json.py."""

from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path

import pytest

# Script filename is hyphenated, which blocks `import daemon.extract_json`.
EXTRACT_PATH = Path(__file__).resolve().parent.parent / "daemon" / "extract-json.py"
_spec = importlib.util.spec_from_file_location("extract_json", EXTRACT_PATH)
assert _spec is not None and _spec.loader is not None
extract_json = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract_json)

ExtractError = extract_json.ExtractError


def _wrap(payload: dict) -> str:
    return f"prose before\n\n```json\n{json.dumps(payload)}\n```\n"


def _minimal_finding(**overrides) -> dict:
    finding = {
        "path": "src/main.py",
        "line": 42,
        "severity": "nit",
        "type": "polish",
        "body": "small naming nit",
    }
    finding.update(overrides)
    return finding


def test_happy_path_returns_review_payload():
    raw = """\
Thinking out loud about the diff here...

```json
{
  "summary": "Solid diff. One naming nit worth flagging before merge.",
  "comments": [
    {
      "path": "src/main.py",
      "line": 42,
      "severity": "nit",
      "type": "polish",
      "body": "`tmp` reads as throwaway. `parsed_payload` would carry the intent."
    }
  ]
}
```
"""
    payload = extract_json.extract(raw)
    assert payload.summary.startswith("Solid diff")
    assert len(payload.comments) == 1
    finding = payload.comments[0]
    assert finding.path == "src/main.py"
    assert finding.line == 42
    assert finding.end_line is None
    assert finding.severity == "nit"
    assert finding.type == "polish"
    assert "parsed_payload" in finding.body


def test_empty_input_raises_empty_stdout():
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract("")
    assert exc_info.value.category == "empty-stdout"


def test_whitespace_only_input_raises_empty_stdout():
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract("   \n  \t  \n")
    assert exc_info.value.category == "empty-stdout"


def test_no_fence_raises_no_fence():
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract("just some prose, no fence here at all\n")
    assert exc_info.value.category == "no-fence"


def test_multiple_fences_picks_last():
    raw = textwrap.dedent(
        """\
        Considering option A:

        ```json
        {"summary": "first draft", "comments": []}
        ```

        Actually, going with option B:

        ```json
        {"summary": "final draft", "comments": []}
        ```
        """
    )
    payload = extract_json.extract(raw)
    assert payload.summary == "final draft"


def test_malformed_json_inside_fence_raises_parse_error():
    raw = "```json\n{not valid json at all\n```\n"
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "parse-error"


def test_missing_summary_raises_schema_invalid():
    raw = _wrap({"comments": []})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "schema-invalid"


def test_missing_comments_defaults_to_empty():
    # Agent intermittently omits `comments` on zero-finding reviews (#44).
    # ReviewPayload defaults comments to [] so the pipeline doesn't trip
    # schema-invalid on a payload like `{"summary": "..."}`.
    raw = _wrap({"summary": "nothing to flag"})
    payload = extract_json.extract(raw)
    assert payload.summary == "nothing to flag"
    assert payload.comments == []


def test_invalid_enum_value_raises_schema_invalid():
    bad = _minimal_finding(severity="critical")  # not in {important, nit, pre_existing}
    raw = _wrap({"summary": "x", "comments": [bad]})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "schema-invalid"


def test_empty_comments_is_valid():
    raw = _wrap({"summary": "nothing to flag", "comments": []})
    payload = extract_json.extract(raw)
    assert payload.summary == "nothing to flag"
    assert payload.comments == []


def test_cap_exceeded_raises_cap_violation():
    comments = [_minimal_finding(line=i) for i in range(1, extract_json.MAX_FINDINGS + 2)]
    raw = _wrap({"summary": "lots", "comments": comments})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "cap-violation"


def test_end_line_equal_to_line_is_valid():
    f = _minimal_finding(line=10, end_line=10)
    raw = _wrap({"summary": "x", "comments": [f]})
    payload = extract_json.extract(raw)
    assert payload.comments[0].end_line == 10


def test_end_line_greater_than_line_is_valid():
    f = _minimal_finding(line=10, end_line=20)
    raw = _wrap({"summary": "x", "comments": [f]})
    payload = extract_json.extract(raw)
    assert payload.comments[0].end_line == 20


def test_end_line_less_than_line_raises_schema_invalid():
    f = _minimal_finding(line=20, end_line=10)
    raw = _wrap({"summary": "x", "comments": [f]})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "schema-invalid"


def test_em_dash_in_summary_raises_style_violation():
    raw = _wrap({"summary": "Solid diff — one nit.", "comments": []})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "style-violation"
    assert "summary" in str(exc_info.value)


def test_em_dash_in_comment_body_raises_style_violation():
    f = _minimal_finding(body="Rename `tmp` — clearer intent.")
    raw = _wrap({"summary": "x", "comments": [f]})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "style-violation"
    assert "comments[0]" in str(exc_info.value)


@pytest.mark.parametrize(
    "opener",
    [
        "This carries the wrong invariant.",
        "The helper reads as dead code.",
        "It would be clearer to split.",
        "Worth splitting into two bullets.",
        "Suggest renaming `tmp`.",
        "Please add a comment here.",
        "Consider splitting this finding.",
        "Maybe rename `tmp`.",
    ],
)
def test_forbidden_body_prefix_raises_style_violation(opener):
    f = _minimal_finding(body=opener)
    raw = _wrap({"summary": "x", "comments": [f]})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "style-violation"


def test_bold_lead_body_passes_style_check():
    # Body shape per ADR 0002 leads with a bold sentence. `**` is forbidden
    # only on summary, not on comments[].body.
    f = _minimal_finding(body="**Rename `tmp` to `parsed_payload`.** Carries the intent.")
    raw = _wrap({"summary": "Solid diff. One nit.", "comments": [f]})
    payload = extract_json.extract(raw)
    assert payload.comments[0].body.startswith("**Rename")


def test_single_bullet_comment_body_raises_style_violation():
    # 2b (#100): a lone bullet is a sentence with extra weight. Bodies carry
    # 0 or 2–4 bullets, never one (review-agent-default §Body shape).
    f = _minimal_finding(body="**Split it.**\n\n- only one point")
    raw = _wrap({"summary": "x", "comments": [f]})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "style-violation"
    assert "comments[0]" in str(exc_info.value)


def test_two_to_four_bullet_comment_body_passes_style_check():
    f = _minimal_finding(body="**Split it.**\n\n- first point\n- second point")
    raw = _wrap({"summary": "x", "comments": [f]})
    payload = extract_json.extract(raw)
    assert payload.comments[0].body.startswith("**Split")


@pytest.mark.parametrize(
    "opener",
    [
        "**This carries the wrong invariant.**",
        "**The helper reads as dead code.**",
        "**It would be clearer to split.**",
        "**Worth splitting into two bullets.**",
        "**Suggest renaming `tmp`.**",
        "**Please add a comment here.**",
        "**Consider splitting this finding.**",
        "**Maybe rename `tmp`.**",
    ],
)
def test_bold_wrapped_forbidden_body_prefix_raises_style_violation(opener):
    # The bold-lead shape (ADR 0002) must not let forbidden openers slip
    # through by hiding behind a leading `**`. Validator peels `**` before
    # the prefix scan; this test pins that.
    f = _minimal_finding(body=opener)
    raw = _wrap({"summary": "x", "comments": [f]})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "style-violation"


@pytest.mark.parametrize(
    "opener",
    [
        "**  This carries the wrong invariant.**",
        "** \tConsider splitting this finding.**",
    ],
)
def test_bold_with_internal_leading_whitespace_still_rejected(opener):
    # `**` was hiding any whitespace inside it from the first lstrip, so a
    # second lstrip after the peel is required. Without it, `**  This …**`
    # would strip to `  This …` and `startswith('This ')` would miss.
    f = _minimal_finding(body=opener)
    raw = _wrap({"summary": "x", "comments": [f]})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "style-violation"


@pytest.mark.parametrize(
    "opener",
    [
        "**bold lead** then prose.",
        "This summary opens demonstratively.",
        "The asymmetry is the load-bearing gap.",
        "It would be clearer to split.",
        "Worth splitting into two bullets.",
        "Suggest renaming `tmp`.",
        "Please add a comment here.",
        "Consider splitting this finding.",
        "Maybe rephrase.",
    ],
)
def test_forbidden_summary_prefix_raises_style_violation(opener):
    raw = _wrap({"summary": opener, "comments": []})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "style-violation"
    assert "summary" in str(exc_info.value)


def test_forbidden_prefix_with_leading_whitespace_still_rejected():
    f = _minimal_finding(body="  This still trips the check.")
    raw = _wrap({"summary": "x", "comments": [f]})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "style-violation"


def test_forbidden_prefix_only_matches_with_trailing_space():
    # `This ` is forbidden; `Thinking` (substring without word boundary) is fine.
    f = _minimal_finding(body="Thinking about the rename, `parsed_payload` reads cleaner.")
    raw = _wrap({"summary": "x", "comments": [f]})
    payload = extract_json.extract(raw)
    assert payload.comments[0].body.startswith("Thinking")


def test_forbidden_prefix_is_case_sensitive():
    # Lowercase `the ` mid-sentence is fine; the check is for sentence openers.
    f = _minimal_finding(body="Rename to match the helper above.")
    raw = _wrap({"summary": "x", "comments": [f]})
    payload = extract_json.extract(raw)
    assert payload.comments[0].body.startswith("Rename")


def test_multiple_style_violations_reported_together():
    f1 = _minimal_finding(line=10, body="This is wrong.")
    f2 = _minimal_finding(line=20, body="Rename with an em dash — like so.")
    raw = _wrap({"summary": "Solid — but...", "comments": [f1, f2]})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "style-violation"
    msg = str(exc_info.value)
    assert "summary" in msg
    assert "comments[0]" in msg
    assert "comments[1]" in msg


@pytest.mark.parametrize(
    "ref",
    [
        "Slice 1",
        "Slice 4",
        "Phase 5",
        "Phase 10",
        "Story #26",
        "PRD #21",
        "PRD 21",
    ],
)
def test_task_ref_in_summary_raises_style_violation(ref):
    raw = _wrap({"summary": f"Clean change. Drops the {ref} comment.", "comments": []})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "style-violation"
    assert "task-scoped ref" in str(exc_info.value)
    assert ref in str(exc_info.value)


@pytest.mark.parametrize(
    "ref",
    [
        "Slice 4",
        "Phase 5",
        "Story #26",
        "PRD #21",
    ],
)
def test_task_ref_in_comment_body_raises_style_violation(ref):
    f = _minimal_finding(body=f"**Drop the {ref} comment.** Rots once the slice ships.")
    raw = _wrap({"summary": "x", "comments": [f]})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "style-violation"
    assert "task-scoped ref" in str(exc_info.value)
    assert "comments[0]" in str(exc_info.value)


@pytest.mark.parametrize(
    "stable_ref",
    [
        "ADR 0006",
        "ADR 0005",
        "RFC 5321",
        "ISO 8601",
    ],
)
def test_stable_refs_pass_style_check(stable_ref):
    # ADR numbers and external standards are stable references the memory rule
    # explicitly allows; the task-ref check must not match them.
    raw = _wrap({"summary": f"Aligns with {stable_ref}. Nothing to flag.", "comments": []})
    payload = extract_json.extract(raw)
    assert stable_ref in payload.summary


@pytest.mark.parametrize(
    "false_positive",
    [
        "slice the array at index 4",  # lowercase 'slice' — common in code prose
        "phase-5",  # hyphenated form used in branch names and tags
        "in a later phase 5 we will",  # lowercase 'phase' in prose
    ],
)
def test_task_ref_check_skips_lowercase_and_hyphenated(false_positive):
    # Case sensitivity is the false-positive guard: title-case `Slice N` /
    # `Phase N` are task-scoped; lowercase or hyphenated forms are not.
    raw = _wrap({"summary": f"Clean change. {false_positive}.", "comments": []})
    payload = extract_json.extract(raw)
    assert false_positive in payload.summary


def test_style_fires_before_cap_when_both_apply():
    # 11 em-dash findings: style is the root cause; cap is downstream noise.
    # Operator should see the voice problem first, not be told to cull one.
    bad = [
        _minimal_finding(line=i, body="Rename `tmp` — clearer intent.")
        for i in range(1, extract_json.MAX_FINDINGS + 2)
    ]
    raw = _wrap({"summary": "x", "comments": bad})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "style-violation"


def test_clean_payload_passes_style_check():
    f = _minimal_finding(body="Rename `tmp` to `parsed_payload`. Carries intent.")
    raw = _wrap({"summary": "Solid diff. One naming nit.", "comments": [f]})
    payload = extract_json.extract(raw)
    assert payload.comments[0].body.startswith("Rename")
