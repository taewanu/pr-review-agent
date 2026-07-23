"""Tests for the Status comment findings index (daemon/findings_index.py, ADR 0020).

The index is a derived, pointer-only view of the PR's daemon Finding threads: a
`total · open · resolved` rollup plus one linked entry per thread (location +
state, never the body). These tests pin the filter (daemon-authored only), the
ordering (open before resolved), the link fallback (no URL → plain label), the
rollup math, and the unanchored pointer that stands in for Review-body findings.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))

import findings_index as fi  # noqa: E402

# The bot's login. Under App identity (ADR 0036) authorship alone identifies the
# bot's own Finding threads, so the index filters on root_author with no body-text
# marker.
OPERATOR = "example[bot]"


def _thread(
    tid: str,
    path: str,
    line: int | None,
    *,
    resolved: bool = False,
    url: str | None = None,
    author: str = OPERATOR,
) -> dict:
    return {
        "thread_id": tid,
        "is_resolved": resolved,
        "root_author": author,
        "root_body": "some finding text",
        "root_comment_id": f"c-{tid}",
        "root_comment_url": url,
        "path": path,
        "original_line": line,
        "original_start_line": None,
        "has_resolution_stamp": resolved,
    }


# --- daemon_findings filter --------------------------------------------------


def test_filter_keeps_only_daemon_authored_findings():
    threads = [
        _thread("1", "a.py", 1),
        _thread("2", "b.py", 2, author="someone-else"),  # another bot / user
        _thread("3", "c.py", 3, author="a-human"),  # a human's manual review comment
    ]
    kept = fi.daemon_findings(threads, OPERATOR)
    assert [t["thread_id"] for t in kept] == ["1"]


def test_filter_keeps_resolved_and_stamped_findings():
    # Unlike resolve_threads.select, the index reports resolved/stamped threads.
    threads = [_thread("1", "a.py", 1, resolved=True)]
    assert len(fi.daemon_findings(threads, OPERATOR)) == 1


# --- _entry: link fallback + state -------------------------------------------


def test_entry_links_location_when_url_present():
    line = fi._entry(_thread("1", "daemon/foo.py", 16, url="https://gh/x#discussion_r1"))
    assert line == "- [`daemon/foo.py:16`](https://gh/x#discussion_r1) · open"


def test_entry_falls_back_to_plain_label_without_url():
    line = fi._entry(_thread("1", "daemon/foo.py", 16, url=None))
    assert line == "- `daemon/foo.py:16` · open"


def test_entry_resolved_state_and_outdated_line():
    # original_line None (thread went outdated) → path-only label.
    line = fi._entry(_thread("1", "daemon/foo.py", None, resolved=True, url="https://gh/x"))
    assert line == "- [`daemon/foo.py`](https://gh/x) · resolved"


# --- render_index: rollup, order, unanchored ---------------------------------


def test_render_rollup_counts_and_open_first_order():
    threads = [
        _thread("r", "z.py", 5, resolved=True, url="https://gh/r"),
        _thread("o", "a.py", 9, url="https://gh/o"),
    ]
    out = fi.render_index(threads, OPERATOR)
    lines = out.splitlines()
    assert lines[0] == "**2 findings · 1 open · 1 resolved**"
    # Open finding precedes the resolved one regardless of path order.
    assert lines[2] == "- [`a.py:9`](https://gh/o) · open"
    assert lines[3] == "- [`z.py:5`](https://gh/r) · resolved"


def test_render_singular_noun_for_one_finding():
    out = fi.render_index([_thread("1", "a.py", 1)], OPERATOR)
    assert out.splitlines()[0] == "**1 finding · 1 open · 0 resolved**"


def test_render_clean_affirmation_bare_rollup_without_summary():
    # Zero findings, no summary: the bare rollup so a clean review still reads as
    # reviewed-and-clean rather than an empty no-op (ADR 0020).
    assert fi.render_index([], OPERATOR, unanchored_count=0) == "**No findings**"


def test_render_clean_affirmation_quotes_the_summary():
    out = fi.render_index(
        [], OPERATOR, unanchored_count=0, summary="All clear; nothing blocking.\n"
    )
    assert out == "**No findings**\n\n> All clear; nothing blocking."


def test_clean_affirmation_quotes_every_line_of_a_multiline_summary():
    # A blockquote breaks out at the first un-prefixed line, so each line (blank
    # lines included) carries its own marker.
    out = fi._clean_affirmation("Line one.\n\nLine two.")
    assert out == "**No findings**\n\n> Line one.\n>\n> Line two."


def test_clean_affirmation_falls_back_to_bare_rollup_on_whitespace_summary():
    assert fi._clean_affirmation("   \n") == "**No findings**"
    assert fi._clean_affirmation(None) == "**No findings**"


def test_render_unanchored_pointer_with_review_url():
    out = fi.render_index([], OPERATOR, unanchored_count=2, review_url="https://gh/review")
    assert out == "_+ 2 outside the diff → [review](https://gh/review)_"


def test_render_unanchored_pointer_without_review_url():
    out = fi.render_index([], OPERATOR, unanchored_count=3, review_url=None)
    assert out == "_+ 3 outside the diff_"


def test_render_findings_then_unanchored_pointer_separated():
    out = fi.render_index(
        [_thread("1", "a.py", 1, url="https://gh/o")],
        OPERATOR,
        unanchored_count=1,
        review_url="https://gh/review",
    )
    assert out.splitlines()[-1] == "_+ 1 outside the diff → [review](https://gh/review)_"
    assert "**1 finding · 1 open · 0 resolved**" in out


# --- _delta_line: per-push delta wording (ADR 0033) --------------------------


def test_delta_line_both_nonzero():
    assert fi._delta_line(fi.Delta(2, 1)) == "_+2 new · 1 fixed_"


def test_delta_line_new_only_carries_plus():
    assert fi._delta_line(fi.Delta(3, 0)) == "_+3 new_"


def test_delta_line_fixed_only_has_no_plus():
    assert fi._delta_line(fi.Delta(0, 2)) == "_2 fixed_"


def test_delta_line_both_zero_reads_no_change():
    # A push that ran and moved nothing is a verdict, not silence (ADR 0033,
    # mirroring ADR 0020 Decision 6's affirmation over a blank slot).
    assert fi._delta_line(fi.Delta(0, 0)) == "_no change_"


# --- render_index: delta placement + suppression -----------------------------


def test_render_prepends_delta_above_rollup():
    out = fi.render_index(
        [_thread("1", "a.py", 1, url="https://gh/o")], OPERATOR, delta=fi.Delta(1, 0)
    )
    lines = out.splitlines()
    # Delta is italic chrome on the first line, rollup (bold) below it.
    assert lines[0] == "_+1 new_"
    assert lines[2] == "**1 finding · 1 open · 0 resolved**"


def test_render_suppresses_delta_when_delta_absent():
    # First review passes no delta (None): the line is omitted entirely, rollup
    # stays the first line.
    out = fi.render_index([_thread("1", "a.py", 1, url="https://gh/o")], OPERATOR)
    assert out.splitlines()[0] == "**1 finding · 1 open · 0 resolved**"


def test_render_no_change_delta_still_shows_on_a_quiet_re_review():
    out = fi.render_index(
        [_thread("1", "a.py", 1, resolved=True, url="https://gh/o")],
        OPERATOR,
        delta=fi.Delta(0, 0),
    )
    lines = out.splitlines()
    assert lines[0] == "_no change_"
    assert lines[2] == "**1 finding · 0 open · 1 resolved**"
