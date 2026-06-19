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

OPERATOR = "operator-login"
TAG = fi.PROVENANCE_MARKER


def _thread(
    tid: str,
    path: str,
    line: int | None,
    *,
    resolved: bool = False,
    url: str | None = None,
    author: str = OPERATOR,
    daemon: bool = True,
) -> dict:
    body = f"some finding text\n\n{TAG}" if daemon else "a human review note"
    return {
        "thread_id": tid,
        "is_resolved": resolved,
        "root_author": author,
        "root_body": body,
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
        _thread("2", "b.py", 2, author="someone-else"),  # not the operator
        _thread("3", "c.py", 3, daemon=False),  # operator, but no provenance marker
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


def test_render_empty_when_no_findings_and_no_unanchored():
    assert fi.render_index([], OPERATOR, unanchored_count=0) == ""


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
