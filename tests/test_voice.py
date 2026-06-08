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
