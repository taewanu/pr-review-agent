"""Tests for daemon/anchor_findings.py."""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

ANCHOR_PATH = Path(__file__).resolve().parent.parent / "daemon" / "anchor_findings.py"
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


def test_parse_binary_file_is_not_recorded():
    # A binary file has a `diff --git` header but no `+++ b/` line, so with the
    # new path read from `+++` the binary file is simply absent. That is inert:
    # is_anchored and match_quote treat an absent path the same as a present-but-
    # empty one, so a finding on a binary file relocates out of the diff either
    # way. The text file that does carry a hunk still records.
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
    assert d.hunks == {"src/foo.py": [(1, 2)]}
    assert not d.is_anchored("assets/logo.png", 1)


def test_parse_quoted_non_ascii_path_anchors():
    # git quotes a non-ASCII path in the `+++` header (octal-escaped UTF-8 bytes);
    # a finding on it must anchor to the new-side hunk, not route out of the diff.
    diff = textwrap.dedent(
        """\
        diff --git "a/w\\303\\256t.py" "b/w\\303\\256t.py"
        --- "a/w\\303\\256t.py"
        +++ "b/w\\303\\256t.py"
        @@ -1,1 +1,2 @@
             keep
        +    add
        """
    )
    d = anchor_findings.Diff.parse(diff)
    assert d.hunks == {"wît.py": [(1, 2)]}
    assert d.is_anchored("wît.py", 2)


def test_parse_path_containing_b_slash_substring_anchors():
    # `a/x b/y.py b/x b/y.py` cannot be split from the `diff --git` line; the
    # `+++` header carries the whole path (tab-terminated for its space).
    diff = textwrap.dedent(
        """\
        diff --git a/x b/y.py b/x b/y.py
        --- a/x b/y.py\t
        +++ b/x b/y.py\t
        @@ -1,1 +1,2 @@
             keep
        +    add
        """
    )
    d = anchor_findings.Diff.parse(diff)
    assert d.hunks == {"x b/y.py": [(1, 2)]}
    assert d.is_anchored("x b/y.py", 2)


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


# ---------- Diff.match_quote ----------


def test_match_quote_finds_new_side_line_by_content():
    diff = textwrap.dedent(
        """\
        diff --git a/src/foo.py b/src/foo.py
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
    # `+    z = 4` is new-side line 12 (context 10, 11; deleted line has no new number).
    assert d.match_quote("src/foo.py", "z = 4") == [12]


def _quote_fixture():
    # New-side lines 1..5: a deleted line carries no new number, so the second
    # `log(x)` lands at 5, not 6.
    diff = textwrap.dedent(
        """\
        diff --git a/src/foo.py b/src/foo.py
        --- a/src/foo.py
        +++ b/src/foo.py
        @@ -1,2 +1,5 @@
        +    total = a + b
        +    log(x)
        +    total = a + b
             keep this
        -    gone
        +    log(x)
        """
    )
    return anchor_findings.Diff.parse(diff)


def test_match_quote_strips_leading_and_trailing_whitespace():
    d = _quote_fixture()
    # Agent drops the indentation; still matches lines 2 and 5 (`    log(x)`).
    assert d.match_quote("src/foo.py", "log(x)") == [2, 5]
    assert d.match_quote("src/foo.py", "  log(x)  ") == [2, 5]


def test_match_quote_preserves_internal_whitespace():
    d = _quote_fixture()
    # Collapsing internal spaces would match `total = a + b`; it must not.
    assert d.match_quote("src/foo.py", "total = a+b") == []
    assert d.match_quote("src/foo.py", "total = a + b") == [1, 3]


def test_match_quote_includes_context_lines():
    d = _quote_fixture()
    # Context (unchanged) lines are new-side anchorable too; line 4 here.
    assert d.match_quote("src/foo.py", "keep this") == [4]


def test_match_quote_no_match_returns_empty():
    d = _quote_fixture()
    assert d.match_quote("src/foo.py", "nonexistent line") == []
    assert d.match_quote("src/other.py", "log(x)") == []


def test_match_quote_empty_quote_returns_empty():
    d = _quote_fixture()
    assert d.match_quote("src/foo.py", "   ") == []


# ---------- numbered_diff (line-numbered diff for the agent, ADR 0018) ----------


def test_numbered_diff_numbers_new_side_lines():
    diff = textwrap.dedent(
        """\
        diff --git a/src/foo.py b/src/foo.py
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
    out = anchor_findings.numbered_diff(diff).splitlines()
    # Context and added lines carry their new-side number.
    assert "10│     x = 1" in out
    assert "11│     y = 2" in out
    assert "12│+    z = 4" in out
    assert "13│     return x" in out
    # The deleted line and the headers carry a blank number column, so `+++` is
    # never mistaken for an added new-side line.
    assert "  │-    z = 3" in out
    assert "  │+++ b/src/foo.py" in out
    assert "  │@@ -10,5 +10,5 @@ def foo():" in out


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


# ---------- resolve_finding (surface routing, ADR 0018 + ADR 0040) ----------


def _surface(finding, d):
    return anchor_findings.resolve_finding(finding, d)[0]


def test_resolve_corrects_wrong_line_on_unique_quote_match():
    d = _quote_fixture()  # "keep this" is uniquely new-side line 4
    finding = {"path": "src/foo.py", "line": 1, "quote": "keep this", "body": "x"}
    surface, resolved = anchor_findings.resolve_finding(finding, d)
    assert surface == anchor_findings.INLINE
    assert resolved["line"] == 4


def test_resolve_no_quote_match_falls_to_file_level_when_path_is_in_the_diff():
    d = _quote_fixture()
    finding = {"path": "src/foo.py", "line": 2, "quote": "not in this diff", "body": "x"}
    assert _surface(finding, d) == anchor_findings.FILE_LEVEL


def test_resolve_falls_to_the_body_when_the_path_is_not_in_the_diff():
    d = _quote_fixture()
    finding = {"path": "src/untouched.py", "line": 2, "quote": "keep this", "body": "x"}
    assert _surface(finding, d) == anchor_findings.BODY


def test_resolve_multi_match_corroborated_by_emitted_line():
    d = _quote_fixture()  # `log(x)` matches new-side lines 2 and 5
    finding = {"path": "src/foo.py", "line": 5, "quote": "log(x)", "body": "x"}
    surface, resolved = anchor_findings.resolve_finding(finding, d)
    assert surface == anchor_findings.INLINE
    assert resolved["line"] == 5


def test_resolve_multi_match_without_corroboration_falls_to_file_level():
    d = _quote_fixture()  # `log(x)` matches 2 and 5; emitted line is neither
    finding = {"path": "src/foo.py", "line": 9, "quote": "log(x)", "body": "x"}
    assert _surface(finding, d) == anchor_findings.FILE_LEVEL


def test_resolve_quote_absent_falls_back_to_range_check():
    d = _quote_fixture()  # hunk covers new-side lines 1..5
    in_hunk = {"path": "src/foo.py", "line": 3, "body": "no quote, in hunk"}
    surface, resolved = anchor_findings.resolve_finding(in_hunk, d)
    assert surface == anchor_findings.INLINE
    assert resolved["line"] == 3
    out_of_hunk = {"path": "src/foo.py", "line": 99, "body": "no quote, outside"}
    assert _surface(out_of_hunk, d) == anchor_findings.FILE_LEVEL


def test_resolve_quote_absent_range_finding_uses_range_check():
    d = _quote_fixture()
    rng = {"path": "src/foo.py", "line": 2, "end_line": 4, "body": "block, no quote"}
    surface, resolved = anchor_findings.resolve_finding(rng, d)
    assert surface == anchor_findings.INLINE
    assert resolved["line"] == 2
    assert resolved["end_line"] == 4


def test_file_level_finding_keeps_its_emitted_line_unverified():
    # ADR 0040: the file-level comment carries no line, so the router neither
    # corrects nor drops the emitted one. It hands the finding back untouched.
    d = _quote_fixture()
    finding = {"path": "src/foo.py", "line": 99, "quote": "nowhere", "body": "x"}
    surface, resolved = anchor_findings.resolve_finding(finding, d)
    assert surface == anchor_findings.FILE_LEVEL
    assert resolved == finding


def _block_fixture():
    # New-side lines 1..7, all in one hunk; start-line quotes are unique.
    diff = textwrap.dedent(
        """\
        diff --git a/src/x.py b/src/x.py
        --- a/src/x.py
        +++ b/src/x.py
        @@ -1,1 +1,7 @@
        +    alpha
        +    bravo
        +    charlie
        +    delta
        +    echo
        +    foxtrot
             base
        """
    )
    return anchor_findings.Diff.parse(diff)


def test_resolve_range_quote_shifts_end_line_by_delta():
    d = _block_fixture()  # `charlie` is uniquely new-side line 3
    # Agent miscounts the block as lines 1..3; the quote pins the start at 3, so
    # the span shifts by +2 to 3..5.
    finding = {"path": "src/x.py", "line": 1, "end_line": 3, "quote": "charlie", "body": "x"}
    surface, resolved = anchor_findings.resolve_finding(finding, d)
    assert surface == anchor_findings.INLINE
    assert resolved["line"] == 3
    assert resolved["end_line"] == 5


def test_resolve_range_quote_falls_to_file_level_when_shift_exits_hunk():
    d = _block_fixture()  # hunk covers 1..7; `foxtrot` is uniquely line 6
    # Shift (+2) pushes end_line to 11, outside the hunk: no inline anchor.
    finding = {"path": "src/x.py", "line": 4, "end_line": 9, "quote": "foxtrot", "body": "x"}
    assert _surface(finding, d) == anchor_findings.FILE_LEVEL


def test_resolve_range_quote_shifts_up_on_negative_delta():
    d = _block_fixture()  # `charlie` is uniquely new-side line 3
    # Agent miscounts the block as lines 10..12; the quote pins the start at 3, so
    # the span shifts by -7 to 3..5 (delta is negative).
    finding = {"path": "src/x.py", "line": 10, "end_line": 12, "quote": "charlie", "body": "x"}
    surface, resolved = anchor_findings.resolve_finding(finding, d)
    assert surface == anchor_findings.INLINE
    assert resolved["line"] == 3
    assert resolved["end_line"] == 5


# ---------- split_findings ----------


def test_split_empty_payload_yields_three_empty_lists():
    d = _hunks_fixture()
    assert anchor_findings.split_findings([], d) == ([], [], [])


def test_split_uses_content_anchoring_and_corrects_line():
    d = _quote_fixture()  # `keep this` is uniquely new-side line 4
    findings = [
        {"path": "src/foo.py", "line": 1, "quote": "keep this", "body": "corrected"},
        {"path": "src/foo.py", "line": 2, "quote": "not present", "body": "file-level"},
    ]
    anchored, file_level, body = anchor_findings.split_findings(findings, d)
    assert [f["body"] for f in anchored] == ["corrected"]
    assert anchored[0]["line"] == 4
    assert [f["body"] for f in file_level] == ["file-level"]
    assert body == []


def test_split_routes_findings_per_anchor_status():
    d = _hunks_fixture()
    findings = [
        {"path": "src/foo.py", "line": 12, "body": "in-hunk"},
        {"path": "src/foo.py", "line": 100, "body": "out-of-hunk"},
        {"path": "src/other.py", "line": 5, "body": "path-not-in-diff"},
        {"path": "src/foo.py", "line": 10, "end_line": 14, "body": "range-in-hunk"},
        {"path": "src/foo.py", "line": 12, "end_line": 51, "body": "range-across-hunks"},
    ]
    anchored, file_level, body = anchor_findings.split_findings(findings, d)
    assert [f["body"] for f in anchored] == ["in-hunk", "range-in-hunk"]
    assert [f["body"] for f in file_level] == ["out-of-hunk", "range-across-hunks"]
    assert [f["body"] for f in body] == ["path-not-in-diff"]


# ---------- count_quote_misses (#191 relocation-cause observation) ----------


def test_quote_miss_count_separates_a_wrong_citation_from_a_region_level_finding():
    # A file-level finding that named a quote cited a line the diff does not
    # contain, which is a generation defect. One that named none had no line to
    # verify in the first place. The ratio is the open question, so the count has
    # to tell them apart.
    file_level = [
        {"path": "a.py", "line": 1, "quote": "not in the diff", "body": "wrong citation"},
        {"path": "a.py", "line": 2, "body": "region-level"},
        {"path": "a.py", "line": 3, "quote": "   ", "body": "blank quote is no quote"},
    ]
    assert anchor_findings.count_quote_misses(file_level) == 1


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


def test_drop_forbidden_combos_drops_pre_existing_intent():
    # ADR 0035: a intent finding compares this PR's description against this
    # PR's diff, so neither side of it can pre-date the change under review.
    findings = [
        {"severity": "pre_existing", "type": "intent", "body": "drop me"},
        {"severity": "important", "type": "intent", "body": "keep me"},
        {"severity": "nit", "type": "intent", "body": "keep me too"},
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
    # so it passes through. Schema validation is extract_json's job, not this
    # step's.
    findings = [{"body": "no severity or type"}]
    kept, dropped = anchor_findings.drop_forbidden_combos(findings)
    assert kept == findings
    assert dropped == 0
