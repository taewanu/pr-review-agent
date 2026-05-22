"""Tests for daemon/anchor-findings.py."""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

ANCHOR_PATH = Path(__file__).resolve().parent.parent / "daemon" / "anchor-findings.py"
_spec = importlib.util.spec_from_file_location("anchor_findings", ANCHOR_PATH)
assert _spec is not None and _spec.loader is not None
anchor_findings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(anchor_findings)


# ---------- Diff.parse ----------


def test_parse_single_file_single_hunk():
    diff = textwrap.dedent(
        """\
        diff --git a/src/foo.py b/src/foo.py
        index abc..def 100644
        --- a/src/foo.py
        +++ b/src/foo.py
        @@ -10,5 +10,5 @@ def foo():
             x = 1
             y = 2
        -    z = 3
        +    z = 4
             return x
        """
    )
    d = anchor_findings.Diff.parse(diff)
    assert d.hunks == {"src/foo.py": [(10, 14)]}


def test_parse_multi_hunk_per_file():
    diff = textwrap.dedent(
        """\
        diff --git a/src/foo.py b/src/foo.py
        index abc..def 100644
        --- a/src/foo.py
        +++ b/src/foo.py
        @@ -10,3 +10,3 @@
             x = 1
        -    y = 2
        +    y = 3
        @@ -50,2 +50,4 @@
             a = 1
        +    b = 2
        +    c = 3
        """
    )
    d = anchor_findings.Diff.parse(diff)
    assert d.hunks == {"src/foo.py": [(10, 12), (50, 53)]}


def test_parse_multi_file():
    diff = textwrap.dedent(
        """\
        diff --git a/src/foo.py b/src/foo.py
        --- a/src/foo.py
        +++ b/src/foo.py
        @@ -1,2 +1,2 @@
        -old
        +new
        diff --git a/src/bar.py b/src/bar.py
        --- a/src/bar.py
        +++ b/src/bar.py
        @@ -10,1 +10,2 @@
             stuff
        +    more
        """
    )
    d = anchor_findings.Diff.parse(diff)
    assert d.hunks == {
        "src/foo.py": [(1, 2)],
        "src/bar.py": [(10, 11)],
    }


def test_parse_rename_uses_new_path():
    diff = textwrap.dedent(
        """\
        diff --git a/src/old_name.py b/src/new_name.py
        similarity index 100%
        rename from src/old_name.py
        rename to src/new_name.py
        --- a/src/old_name.py
        +++ b/src/new_name.py
        @@ -5,1 +5,2 @@
             keep
        +    add
        """
    )
    d = anchor_findings.Diff.parse(diff)
    assert d.hunks == {"src/new_name.py": [(5, 6)]}
    assert "src/old_name.py" not in d.hunks


def test_parse_binary_file_records_path_with_no_hunks():
    diff = textwrap.dedent(
        """\
        diff --git a/assets/logo.png b/assets/logo.png
        index abc..def 100644
        Binary files a/assets/logo.png and b/assets/logo.png differ
        diff --git a/src/foo.py b/src/foo.py
        --- a/src/foo.py
        +++ b/src/foo.py
        @@ -1,1 +1,2 @@
             keep
        +    add
        """
    )
    d = anchor_findings.Diff.parse(diff)
    assert d.hunks == {
        "assets/logo.png": [],
        "src/foo.py": [(1, 2)],
    }


def test_parse_empty_count_variant():
    # `@@ -40 +40 @@` (no comma) means count=1
    diff = textwrap.dedent(
        """\
        diff --git a/src/foo.py b/src/foo.py
        --- a/src/foo.py
        +++ b/src/foo.py
        @@ -40 +40 @@
        -old
        +new
        """
    )
    d = anchor_findings.Diff.parse(diff)
    assert d.hunks == {"src/foo.py": [(40, 40)]}


def test_parse_dev_null_added_file():
    diff = textwrap.dedent(
        """\
        diff --git a/src/new.py b/src/new.py
        new file mode 100644
        index 0000000..abc
        --- /dev/null
        +++ b/src/new.py
        @@ -0,0 +1,3 @@
        +line1
        +line2
        +line3
        """
    )
    d = anchor_findings.Diff.parse(diff)
    assert d.hunks == {"src/new.py": [(1, 3)]}


# ---------- Diff.is_anchored ----------


def _hunks_fixture():
    return anchor_findings.Diff(
        hunks={
            "src/foo.py": [(10, 14), (50, 53)],
            "assets/logo.png": [],  # binary
        }
    )


def test_anchored_single_line_inside_hunk():
    d = _hunks_fixture()
    assert d.is_anchored("src/foo.py", 12)
    assert d.is_anchored("src/foo.py", 50)
    assert d.is_anchored("src/foo.py", 53)


def test_anchored_single_line_outside_any_hunk():
    d = _hunks_fixture()
    assert not d.is_anchored("src/foo.py", 9)
    assert not d.is_anchored("src/foo.py", 15)
    assert not d.is_anchored("src/foo.py", 49)
    assert not d.is_anchored("src/foo.py", 100)


def test_anchored_path_not_in_diff():
    d = _hunks_fixture()
    assert not d.is_anchored("src/other.py", 10)


def test_anchored_binary_file_never_anchors():
    d = _hunks_fixture()
    assert not d.is_anchored("assets/logo.png", 1)


def test_anchored_range_within_single_hunk():
    d = _hunks_fixture()
    assert d.is_anchored("src/foo.py", 10, end_line=14)
    assert d.is_anchored("src/foo.py", 11, end_line=13)


def test_anchored_range_spanning_two_hunks():
    d = _hunks_fixture()
    # 12 in first hunk, 51 in second hunk — different hunks
    assert not d.is_anchored("src/foo.py", 12, end_line=51)


def test_anchored_range_partially_outside():
    d = _hunks_fixture()
    # start in hunk, end past it
    assert not d.is_anchored("src/foo.py", 12, end_line=20)
    # start before hunk, end inside
    assert not d.is_anchored("src/foo.py", 5, end_line=12)


def test_anchored_range_equal_endpoints_treated_as_single_line():
    d = _hunks_fixture()
    assert d.is_anchored("src/foo.py", 12, end_line=12)
    assert not d.is_anchored("src/foo.py", 9, end_line=9)


# ---------- split_findings ----------


def test_split_empty_payload_yields_two_empty_lists():
    d = _hunks_fixture()
    anchored, unanchored = anchor_findings.split_findings([], d)
    assert anchored == []
    assert unanchored == []


def test_split_routes_findings_per_anchor_status():
    d = _hunks_fixture()
    findings = [
        {"path": "src/foo.py", "line": 12, "body": "in-hunk"},
        {"path": "src/foo.py", "line": 100, "body": "out-of-hunk"},
        {"path": "src/other.py", "line": 5, "body": "path-not-in-diff"},
        {"path": "src/foo.py", "line": 10, "end_line": 14, "body": "range-in-hunk"},
        {"path": "src/foo.py", "line": 12, "end_line": 51, "body": "range-across-hunks"},
    ]
    anchored, unanchored = anchor_findings.split_findings(findings, d)
    assert [f["body"] for f in anchored] == ["in-hunk", "range-in-hunk"]
    assert [f["body"] for f in unanchored] == [
        "out-of-hunk",
        "path-not-in-diff",
        "range-across-hunks",
    ]


# ---------- drop_forbidden_combos ----------


def test_drop_forbidden_combos_empty():
    kept, dropped = anchor_findings.drop_forbidden_combos([])
    assert kept == []
    assert dropped == 0


def test_drop_forbidden_combos_drops_important_polish():
    findings = [
        {"severity": "important", "type": "polish", "body": "drop me"},
        {"severity": "nit", "type": "polish", "body": "keep me"},
        {"severity": "important", "type": "bug", "body": "keep me too"},
    ]
    kept, dropped = anchor_findings.drop_forbidden_combos(findings)
    assert [f["body"] for f in kept] == ["keep me", "keep me too"]
    assert dropped == 1


def test_drop_forbidden_combos_counts_multiple():
    findings = [
        {"severity": "important", "type": "polish", "body": "drop 1"},
        {"severity": "important", "type": "polish", "body": "drop 2"},
        {"severity": "nit", "type": "refactor", "body": "keep"},
    ]
    kept, dropped = anchor_findings.drop_forbidden_combos(findings)
    assert [f["body"] for f in kept] == ["keep"]
    assert dropped == 2


def test_drop_forbidden_combos_missing_fields_treated_as_non_match():
    # A malformed finding with missing severity/type isn't in FORBIDDEN_COMBOS
    # so it passes through. Schema validation is extract-json's job, not this
    # step's.
    findings = [{"body": "no severity or type"}]
    kept, dropped = anchor_findings.drop_forbidden_combos(findings)
    assert kept == findings
    assert dropped == 0
