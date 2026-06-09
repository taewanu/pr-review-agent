"""Tests for daemon/voice.py — shared post-hoc voice checks (ADR 0010).

The opener / em-dash / task-ref rules used to live only in extract-json.py.
ADR 0010 moves them here so post_reply.py can enforce the same rules on reply
bodies. These tests pin the shared API directly; the review path keeps its own
behavior tests in test_extract_json.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

VOICE_PATH = Path(__file__).resolve().parent.parent / "daemon" / "voice.py"
_spec = importlib.util.spec_from_file_location("voice", VOICE_PATH)
assert _spec is not None and _spec.loader is not None
voice = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(voice)


# --- forbidden_prefix --------------------------------------------------------


def test_forbidden_prefix_matches_and_returns_the_prefix():
    assert voice.forbidden_prefix("This carries a bug.", voice.FORBIDDEN_PREFIXES) == "This "


def test_forbidden_prefix_none_when_clean():
    assert voice.forbidden_prefix("Rename `tmp`.", voice.FORBIDDEN_PREFIXES) is None


def test_forbidden_prefix_is_case_sensitive():
    assert voice.forbidden_prefix("the helper reads cleaner.", voice.FORBIDDEN_PREFIXES) is None


def test_forbidden_prefix_requires_word_boundary_trailing_space():
    assert voice.forbidden_prefix("Thinking aloud about it.", voice.FORBIDDEN_PREFIXES) is None


def test_forbidden_prefix_strip_bold_peels_then_matches():
    assert (
        voice.forbidden_prefix("**This is wrong.**", voice.FORBIDDEN_PREFIXES, strip_bold=True)
        == "This "
    )


def test_forbidden_prefix_strip_bold_handles_internal_leading_whitespace():
    assert (
        voice.forbidden_prefix("**  This is wrong.**", voice.FORBIDDEN_PREFIXES, strip_bold=True)
        == "This "
    )


def test_forbidden_prefix_without_strip_bold_does_not_peel():
    # A bold lead is only caught when strip_bold is set; the bare `**` opener is
    # forbidden separately via FORBIDDEN_SUMMARY_PREFIXES.
    assert voice.forbidden_prefix("**This is wrong.**", voice.FORBIDDEN_PREFIXES) is None


def test_summary_prefixes_forbid_a_leading_bold():
    assert (
        voice.forbidden_prefix("**bold lead** then prose.", voice.FORBIDDEN_SUMMARY_PREFIXES)
        == "**"
    )


def test_summary_prefixes_superset_of_body_prefixes():
    assert voice.FORBIDDEN_SUMMARY_PREFIXES == ("**",) + voice.FORBIDDEN_PREFIXES


# --- split_bold_lead ---------------------------------------------------------


def test_split_bold_lead_peels_lead_and_lstrips_rest():
    assert voice.split_bold_lead("**Confirmed.** The guard covers it.") == (
        "**Confirmed.**",
        "The guard covers it.",
    )


def test_split_bold_lead_no_rest_when_lead_is_whole_body():
    assert voice.split_bold_lead("**Confirmed by deletion.**") == ("**Confirmed by deletion.**", "")


def test_split_bold_lead_returns_body_whole_when_no_bold_opener():
    assert voice.split_bold_lead("plain prose, no lead") == ("", "plain prose, no lead")


def test_split_bold_lead_returns_body_whole_when_opener_has_no_closer():
    # A stray `**` with no closing delimiter is not a lead; keep the body intact.
    assert voice.split_bold_lead("**dangling opener and prose") == (
        "",
        "**dangling opener and prose",
    )


def test_split_bold_lead_keeps_inner_bold_in_the_rest():
    # The first closing `**` ends the lead; later bold spans stay in the prose.
    assert voice.split_bold_lead("**Lead.** then **inner** bold") == (
        "**Lead.**",
        "then **inner** bold",
    )


# --- find_task_ref -----------------------------------------------------------


def test_find_task_ref_matches_and_returns_the_ref():
    assert voice.find_task_ref("Drops the Slice 4 comment.") == "Slice 4"


def test_find_task_ref_none_for_stable_refs():
    assert voice.find_task_ref("References ADR 0002 and RFC 822.") is None


# --- check_text (the shared per-field helper) --------------------------------


def test_check_text_clean_returns_empty_list():
    assert voice.check_text("Rename `tmp`.", prefixes=voice.FORBIDDEN_PREFIXES, label="body") == []


def test_check_text_em_dash_message_carries_label():
    out = voice.check_text(
        "Rename — now.", prefixes=voice.FORBIDDEN_PREFIXES, label="comments[2].body"
    )
    assert out == ["comments[2].body contains em dash"]


def test_check_text_forbidden_prefix_message_carries_label_and_word():
    out = voice.check_text(
        "**This is wrong.**", prefixes=voice.FORBIDDEN_PREFIXES, strip_bold=True, label="body"
    )
    assert out == ["body opens with forbidden prefix 'This'"]


def test_check_text_task_ref_message_carries_label_and_ref():
    out = voice.check_text(
        "Drops the Slice 4 note.", prefixes=voice.FORBIDDEN_PREFIXES, label="summary"
    )
    assert out == ["summary contains task-scoped ref 'Slice 4'"]


def test_check_text_reports_all_three_violations_together():
    out = voice.check_text(
        "This drops the Slice 4 note — really.", prefixes=voice.FORBIDDEN_PREFIXES, label="summary"
    )
    assert any("em dash" in v for v in out)
    assert any("forbidden prefix" in v for v in out)
    assert any("task-scoped ref" in v for v in out)


# --- bullet_count_violation (2b structural shape, #100) ----------------------

_LEAD = "**Drop it.**"
_BODY = lambda *bullets: "\n\n".join([_LEAD, *(f"- {b}" for b in bullets)])  # noqa: E731


def test_bullet_count_none_when_no_bullets():
    # A plain or short body (0 bullets) is the prescribed single-sentence form.
    assert voice.bullet_count_violation(_LEAD) is None


def test_bullet_count_violation_on_exactly_one():
    msg = voice.bullet_count_violation(_BODY("only one point"))
    assert msg is not None and "single bullet" in msg


def test_bullet_count_none_for_two_to_four():
    for n in (2, 3, 4):
        assert voice.bullet_count_violation(_BODY(*[f"p{i}" for i in range(n)])) is None


def test_bullet_count_violation_on_five_or_more():
    msg = voice.bullet_count_violation(_BODY(*[f"p{i}" for i in range(5)]))
    assert msg is not None and "5" in msg


def test_bullet_count_ignores_indented_and_inline_hyphens():
    # Only a column-0 `- ` marker counts: an indented continuation and a prose
    # hyphen are not bullets, so this single-point body stays clean.
    body = "**Drop it.** It is a no-op;\n  - not a bullet, a wrapped clause."
    assert voice.bullet_count_violation(body) is None


# --- check_text bullet wiring (#100) -----------------------------------------


def test_check_text_bullets_flag_off_by_default_ignores_lone_bullet():
    # summary path passes check_bullets=False, so a single `- ` line never trips.
    assert voice.check_text(_BODY("lone"), prefixes=voice.FORBIDDEN_PREFIXES, label="summary") == []


def test_check_text_bullets_flag_on_flags_lone_bullet_with_label():
    out = voice.check_text(
        _BODY("lone"),
        prefixes=voice.FORBIDDEN_PREFIXES,
        strip_bold=True,
        check_bullets=True,
        label="comments[1].body",
    )
    assert len(out) == 1 and out[0].startswith("comments[1].body ")
    assert "single bullet" in out[0]


def test_check_text_bullets_flag_on_passes_two_to_four():
    out = voice.check_text(
        _BODY("a", "b"),
        prefixes=voice.FORBIDDEN_PREFIXES,
        strip_bold=True,
        check_bullets=True,
        label="replies[0].body",
    )
    assert out == []
