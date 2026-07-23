"""Tests for daemon/voice.py — shared post-hoc voice checks (ADR 0010).

The opener / em-dash / task-ref rules used to live only in extract_json.py.
ADR 0010 moves them here so create_reply.py can enforce the same rules on reply
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


def test_forbidden_prefix_strip_bold_peels_italic_lead():
    # A forbidden opener hidden in an italic lead trips just like a bold one.
    assert (
        voice.forbidden_prefix("_This is wrong_", voice.FORBIDDEN_PREFIXES, strip_bold=True)
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


# --- split_lead --------------------------------------------------------------


def test_split_lead_peels_bold_lead_and_lstrips_rest():
    assert voice.split_lead("**Confirmed.** The guard covers it.") == (
        "**Confirmed.**",
        "The guard covers it.",
    )


def test_split_lead_underscores_in_bold_lead_unaffected():
    # snake_case `_` inside a bold lead must not close the bold span early.
    assert voice.split_lead("**Split `parse_and_persist` here.**") == (
        "**Split `parse_and_persist` here.**",
        "",
    )


def test_split_lead_no_rest_when_lead_is_whole_body():
    assert voice.split_lead("**Confirmed by deletion.**") == ("**Confirmed by deletion.**", "")


def test_split_lead_peels_italic_lead_and_lstrips_rest():
    assert voice.split_lead("_Confirmed:_ trailing") == ("_Confirmed:_", "trailing")


def test_split_lead_skips_snake_case_underscore_in_italic_lead():
    # An intra-word `_` is not a closer (CommonMark right-flanking); the lead
    # closes at the final `_` at end of body, not the one inside some_helper.
    assert voice.split_lead("_drop the unused some_helper import_") == (
        "_drop the unused some_helper import_",
        "",
    )


def test_split_lead_italic_lead_with_inline_code_and_rest():
    assert voice.split_lead("_`session.token` still emitted_ extra") == (
        "_`session.token` still emitted_",
        "extra",
    )


def test_split_lead_returns_body_whole_when_no_opener():
    assert voice.split_lead("plain prose, no lead") == ("", "plain prose, no lead")


def test_split_lead_returns_body_whole_when_bold_opener_has_no_closer():
    # A stray `**` with no closing delimiter is not a lead; keep the body intact.
    assert voice.split_lead("**dangling bold") == ("", "**dangling bold")


def test_split_lead_returns_body_whole_when_italic_opener_has_no_closer():
    assert voice.split_lead("_dangling italic") == ("", "_dangling italic")


def test_split_lead_keeps_inner_bold_in_the_rest():
    # The first closing `**` ends the lead; later bold spans stay in the prose.
    assert voice.split_lead("**Lead.** then **inner** bold") == (
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


# --- fidelity_violation (#133, ADR 0016) -------------------------------------


def test_fidelity_flags_html_escaped_lt_even_inside_a_code_span():
    # The trial corruption put &lt; inside a code span where raw < belonged.
    msg = voice.fidelity_violation("**Cover it.** The `scrollTop &lt;= 0` gate is untested.")
    assert msg is not None and "&lt;" in msg


def test_fidelity_flags_escaped_gt_and_amp():
    assert voice.fidelity_violation("a &gt; b") is not None
    assert voice.fidelity_violation("read a &amp; b") is not None


def test_fidelity_flags_literal_backslash_n_outside_a_code_span():
    msg = voice.fidelity_violation("Summary.\\n\\n- a point")
    assert msg is not None and "backslash-n" in msg


def test_fidelity_allows_backslash_n_inside_a_code_span():
    # A body legitimately showing the escape sequence as code is not a corruption.
    assert voice.fidelity_violation("**Match the newline.** The regex needs `\\n` here.") is None


def test_fidelity_allows_real_newlines_and_raw_angle_brackets():
    assert voice.fidelity_violation("**Fix it.**\n\n- a < b holds\n- ship it") is None


def test_fidelity_clean_returns_none():
    assert voice.fidelity_violation("**Rename `tmp`.** It shadows the builtin.") is None


# --- check_text check_fidelity flag ------------------------------------------


def test_check_text_fidelity_off_by_default():
    out = voice.check_text("a &lt; b", prefixes=voice.FORBIDDEN_PREFIXES, label="x")
    assert out == []


def test_check_text_fidelity_on_flags_and_labels():
    out = voice.check_text(
        "a &lt; b", prefixes=voice.FORBIDDEN_PREFIXES, check_fidelity=True, label="comments[0].body"
    )
    assert len(out) == 1 and out[0].startswith("comments[0].body ")


# --- check_payload -----------------------------------------------------------


def test_check_payload_clean():
    assert voice.check_payload("Renamed two helpers.", ["**Drop the token log.** It leaks."]) == []


def test_check_payload_flags_summary_and_body():
    out = voice.check_payload("This change is risky.", ["**Fix it.** It opens cleanly."])
    assert any(v.startswith("summary ") and "forbidden prefix" in v for v in out)


def test_check_payload_no_longer_checks_fidelity():
    # Fidelity moved to fidelity_violations; check_payload sees only cosmetics, so
    # an escaped entity in an otherwise clean payload is not its concern.
    assert voice.check_payload("Summary holds.", ["**Cover `a &lt;= b`.** Untested."]) == []


def test_check_payload_body_index_in_label():
    out = voice.check_payload("Clean lead.", ["**Good.** Ships.", "**Bad.**\n\n- lone"])
    assert any(v.startswith("comments[1].body ") for v in out)


# --- fidelity_violations (the plural wrapper, the post-Editor fail-closed set) ---


def test_fidelity_violations_clean_payload_is_empty():
    assert voice.fidelity_violations("Summary holds.", ["**Good.** Ships clean."]) == []


def test_fidelity_violations_flags_a_corrupt_body_with_index():
    out = voice.fidelity_violations("Summary holds.", ["**Cover `a &lt;= b`.** Untested."])
    assert len(out) == 1 and out[0].startswith("comments[0].body ")


def test_fidelity_violations_flags_a_corrupt_summary():
    # The case the post-Editor gate most wants fail-closed: the Editor HTML-escapes
    # the reconciled summary while every body stays clean.
    out = voice.fidelity_violations("Cover `a &lt;= b` in the guard.", ["**Good.** Ships clean."])
    assert len(out) == 1 and out[0].startswith("summary ")


# --- check_artifact (the per-artifact rule matrix, ADR 0010 §4) ---------------


def test_artifact_rules_cover_every_artifact_constant():
    # Adding an artifact constant without a rule set (or vice versa) fails here.
    assert set(voice._ARTIFACT_RULES) == {
        voice.SUMMARY,
        voice.INLINE_COMMENT,
        voice.REPLY_BODY,
        voice.RESOLUTION_STAMP,
    }


def test_summary_forbids_a_bold_lead_and_skips_the_bullet_count():
    # FORBIDDEN_SUMMARY_PREFIXES adds "**": the summary stays plain prose, no lead.
    assert voice.check_artifact(voice.SUMMARY, "**Bold** opener.")
    # A single bullet is fine: the summary is not held to the 2-4 body count.
    assert voice.check_artifact(voice.SUMMARY, "Fix the leak.\n\n- one point") == []


def test_inline_comment_peels_a_bold_lead_and_enforces_the_bullet_count():
    # strip_bold peels the bold lead before the opener scan, so a bold lead is clean.
    assert voice.check_artifact(voice.INLINE_COMMENT, "**Rename** `tmp` for clarity.") == []
    # One bullet violates the 2-4 count (0 or 2-4, never one).
    assert voice.check_artifact(voice.INLINE_COMMENT, "**Fix** it.\n\n- only one")


def test_reply_body_peels_an_italic_lead_and_enforces_the_bullet_count():
    # Reply leads are italic (ADR 0010 §4 #106); strip_bold peels `_…_` too (#104).
    assert voice.check_artifact(voice.REPLY_BODY, "_Confirmed:_ the fix lands.") == []
    assert voice.check_artifact(voice.REPLY_BODY, "_Fixed:_ done.\n\n- only one")


def test_resolution_stamp_rule_set():
    # A distinct combo: the body opener set and the bullet count, but no lead peel
    # (a stamp rationale is plain text). Pinned directly so a swap fails here.
    assert voice._ARTIFACT_RULES[voice.RESOLUTION_STAMP] == {
        "prefixes": voice.FORBIDDEN_PREFIXES,
        "strip_bold": False,
        "check_bullets": True,
    }
    assert voice.check_artifact(voice.RESOLUTION_STAMP, "This is now fixed.")  # opener
    assert voice.check_artifact(voice.RESOLUTION_STAMP, "Fixed at HEAD.\n\n- one")  # bullets
    assert voice.check_artifact(voice.RESOLUTION_STAMP, "Fixed the guard at HEAD.") == []


def test_every_artifact_flags_em_dash_and_task_ref():
    for artifact in (voice.SUMMARY, voice.INLINE_COMMENT, voice.REPLY_BODY, voice.RESOLUTION_STAMP):
        assert voice.check_artifact(artifact, "a — b"), artifact
        assert voice.check_artifact(artifact, "See Slice 3 for context."), artifact


def test_check_artifact_label_defaults_to_the_artifact_name():
    out = voice.check_artifact(voice.INLINE_COMMENT, "a — b")
    assert out and out[0].startswith(f"{voice.INLINE_COMMENT} ")
