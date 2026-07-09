"""Tests for daemon/extract_json.py."""

from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path

import pytest

# daemon scripts aren't an importable package; load the module by path.
EXTRACT_PATH = Path(__file__).resolve().parent.parent / "daemon" / "extract_json.py"
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


def test_invalid_enum_value_drops_that_finding_only(capsys):
    # A single malformed finding no longer fails the whole payload (dogfood
    # bug: sounds-abroad#165's tests lens had one valid, 90-confidence finding
    # dropped alongside an unrelated one with severity="minor").
    bad = _minimal_finding(severity="critical")  # not in {important, nit, pre_existing}
    good = _minimal_finding(line=99)
    raw = _wrap({"summary": "x", "comments": [bad, good]})
    payload = extract_json.extract(raw)
    assert len(payload.comments) == 1
    assert payload.comments[0].line == 99
    assert "finding-skip: comments[0] failed validation" in capsys.readouterr().err


def test_all_findings_invalid_leaves_empty_comments():
    bad = _minimal_finding(severity="critical")
    raw = _wrap({"summary": "x", "comments": [bad]})
    payload = extract_json.extract(raw)
    assert payload.comments == []


def test_summary_not_a_string_still_fails_whole_payload():
    # Payload-level fields have no per-item structure to salvage.
    raw = _wrap({"summary": 123, "comments": []})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "schema-invalid"


def test_comments_not_a_list_fails_whole_payload():
    raw = _wrap({"summary": "x", "comments": "not a list"})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "schema-invalid"


def test_empty_comments_is_valid():
    raw = _wrap({"summary": "nothing to flag", "comments": []})
    payload = extract_json.extract(raw)
    assert payload.summary == "nothing to flag"
    assert payload.comments == []


def test_cap_exceeded_raises_cap_violation():
    comments = [_minimal_finding(line=i) for i in range(1, extract_json.max_findings() + 2)]
    raw = _wrap({"summary": "lots", "comments": comments})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "cap-violation"


def test_max_findings_env_overrides_cap(monkeypatch):
    # #199: the cap is an operator tunable via env MAX_FINDINGS, wired the same
    # way as CONFIDENCE_THRESHOLD (ADR 0022), replacing the dead
    # .pr-review.yaml max_findings key that was parsed but never read.
    monkeypatch.setenv("MAX_FINDINGS", "2")
    comments = [_minimal_finding(line=i) for i in range(1, 4)]
    raw = _wrap({"summary": "lots", "comments": comments})
    with pytest.raises(ExtractError) as exc_info:
        extract_json.extract(raw)
    assert exc_info.value.category == "cap-violation"


def test_max_findings_non_integer_falls_back_with_warning(monkeypatch, capsys):
    # Same degradation contract as CONFIDENCE_THRESHOLD: an operator typo must
    # not crash every review tick, so the default applies and the typo is said
    # out loud on stderr.
    monkeypatch.setenv("MAX_FINDINGS", "many")
    comments = [_minimal_finding(line=i) for i in range(1, 4)]
    raw = _wrap({"summary": "ok", "comments": comments})
    payload = extract_json.extract(raw)
    assert len(payload.comments) == 3
    assert "MAX_FINDINGS" in capsys.readouterr().err


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


def test_quote_survives_extraction():
    # The agent's quoted line text must reach the payload so anchor_findings can
    # content-anchor it (ADR 0018); pydantic drops unknown fields by default.
    f = _minimal_finding(quote="    total = a + b")
    raw = _wrap({"summary": "x", "comments": [f]})
    payload = extract_json.extract(raw)
    assert payload.comments[0].quote == "    total = a + b"


def test_quote_defaults_to_none_when_omitted():
    f = _minimal_finding()
    raw = _wrap({"summary": "x", "comments": [f]})
    payload = extract_json.extract(raw)
    assert payload.comments[0].quote is None


def test_end_line_less_than_line_drops_that_finding_only():
    bad = _minimal_finding(line=20, end_line=10)
    good = _minimal_finding(line=30)
    raw = _wrap({"summary": "x", "comments": [bad, good]})
    payload = extract_json.extract(raw)
    assert len(payload.comments) == 1
    assert payload.comments[0].line == 30


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
        for i in range(1, extract_json.max_findings() + 2)
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


# --- validate_style toggle (#133, ADR 0016) ----------------------------------


def _style_breaking_payload() -> dict:
    # Forbidden summary opener + an em dash in a body: fails the gate by default.
    return {
        "summary": "This change is risky.",
        "comments": [
            {
                "path": "a.py",
                "line": 1,
                "severity": "nit",
                "type": "polish",
                "body": "**Fix it.** It breaks — here.",
            }
        ],
    }


def test_extract_default_validates_style():
    with pytest.raises(ExtractError) as exc:
        extract_json.extract(_wrap(_style_breaking_payload()))
    assert exc.value.category == "style-violation"


def test_extract_no_style_skips_the_gate():
    payload = extract_json.extract(_wrap(_style_breaking_payload()), validate_style=False)
    assert payload.summary == "This change is risky."
    assert payload.comments[0].body.endswith("breaks — here.")


def test_extract_no_style_still_enforces_cap():
    big = {
        "summary": "Many findings.",
        "comments": [
            {
                "path": f"f{i}.py",
                "line": 1,
                "severity": "nit",
                "type": "polish",
                "body": f"**Item {i}.** Ok.",
            }
            for i in range(11)
        ],
    }
    with pytest.raises(ExtractError) as exc:
        extract_json.extract(_wrap(big), validate_style=False)
    assert exc.value.category == "cap-violation"


# --- confidence field + gate (ADR 0022) --------------------------------------


def test_confidence_survives_extraction():
    # Like `quote`, the score must reach the payload; pydantic drops unknown keys.
    f = _minimal_finding(confidence=90)
    payload = extract_json.extract(_wrap({"summary": "x", "comments": [f]}))
    assert payload.comments[0].confidence == 90


def test_confidence_defaults_to_none_when_omitted():
    f = _minimal_finding()
    payload = extract_json.extract(_wrap({"summary": "x", "comments": [f]}))
    assert payload.comments[0].confidence is None


@pytest.mark.parametrize("bad", [-1, 101, 150])
def test_confidence_out_of_range_drops_that_finding_only(bad):
    f = _minimal_finding(confidence=bad)
    good = _minimal_finding(line=99)
    payload = extract_json.extract(_wrap({"summary": "x", "comments": [f, good]}))
    assert len(payload.comments) == 1
    assert payload.comments[0].line == 99


def test_gate_drops_below_threshold(monkeypatch):
    monkeypatch.delenv("CONFIDENCE_THRESHOLD", raising=False)  # default 80
    f = _minimal_finding(confidence=79)
    payload = extract_json.extract(_wrap({"summary": "x", "comments": [f]}))
    assert payload.comments == []


def test_gate_keeps_at_or_above_threshold(monkeypatch):
    monkeypatch.delenv("CONFIDENCE_THRESHOLD", raising=False)  # default 80
    at = _minimal_finding(line=1, confidence=80)
    above = _minimal_finding(line=2, confidence=95)
    payload = extract_json.extract(_wrap({"summary": "x", "comments": [at, above]}))
    assert [c.confidence for c in payload.comments] == [80, 95]


def test_gate_keeps_unscored_none(monkeypatch):
    # Absence means not-scored, not zero: an unscored finding is never culled,
    # preserving older payloads and #44 omissions.
    monkeypatch.delenv("CONFIDENCE_THRESHOLD", raising=False)
    f = _minimal_finding(confidence=None)
    payload = extract_json.extract(_wrap({"summary": "x", "comments": [f]}))
    assert len(payload.comments) == 1
    assert payload.comments[0].confidence is None


def test_gate_respects_env_override(monkeypatch):
    # A finding the default (80) would drop survives when the operator lowers
    # the threshold: the gate reads the env at call time.
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "50")
    f = _minimal_finding(confidence=60)
    payload = extract_json.extract(_wrap({"summary": "x", "comments": [f]}))
    assert payload.comments[0].confidence == 60


def test_gate_drops_before_cap(monkeypatch):
    # 11 findings, 5 below threshold: the gate culls them first, so the 6 that
    # survive fall under the cap and no cap-violation fires (drop-then-cap).
    monkeypatch.delenv("CONFIDENCE_THRESHOLD", raising=False)
    low = [_minimal_finding(line=i, confidence=10) for i in range(1, 6)]
    high = [_minimal_finding(line=i, confidence=90) for i in range(6, 12)]
    payload = extract_json.extract(_wrap({"summary": "x", "comments": low + high}))
    assert len(payload.comments) == 6


def test_gate_runs_under_no_style(monkeypatch):
    # The daemon calls extract with --no-style; the gate must still run there.
    monkeypatch.delenv("CONFIDENCE_THRESHOLD", raising=False)
    f = _minimal_finding(confidence=10)
    payload = extract_json.extract(_wrap({"summary": "x", "comments": [f]}), validate_style=False)
    assert payload.comments == []


@pytest.mark.parametrize("garbage", ["", "high", "80.5", "  "])
def test_malformed_threshold_falls_back_to_default(monkeypatch, garbage):
    # A non-integer env value must not escape as an uncaught crash; it falls
    # back to the 80 default so one operator typo doesn't break every tick.
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", garbage)
    below = _minimal_finding(line=1, confidence=50)
    at = _minimal_finding(line=2, confidence=80)
    payload = extract_json.extract(_wrap({"summary": "x", "comments": [below, at]}))
    assert [c.confidence for c in payload.comments] == [80]  # default 80 applied


def test_out_of_range_threshold_is_honored(monkeypatch):
    # An in-range-integer-but-extreme threshold is a legitimate operator choice,
    # not malformed: 0 keeps everything, so it is honored as-is.
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "0")
    f = _minimal_finding(confidence=1)
    payload = extract_json.extract(_wrap({"summary": "x", "comments": [f]}))
    assert payload.comments[0].confidence == 1


# --- parse_no_style_flag (shared by this module's main() and merge_findings.py) --


def test_parse_no_style_flag_present_sets_false_and_strips_it():
    validate_style, args = extract_json.parse_no_style_flag(["--no-style", "payload.txt"])
    assert validate_style is False
    assert args == ["payload.txt"]


def test_parse_no_style_flag_absent_defaults_true():
    validate_style, args = extract_json.parse_no_style_flag(["payload.txt"])
    assert validate_style is True
    assert args == ["payload.txt"]


def test_parse_no_style_flag_strips_from_any_position():
    validate_style, args = extract_json.parse_no_style_flag(["a.txt", "--no-style", "b.txt"])
    assert validate_style is False
    assert args == ["a.txt", "b.txt"]
