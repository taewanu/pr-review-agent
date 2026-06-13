"""Tests for daemon/resolve_threads.py — commit-driven resolution (#125, ADR 0017)."""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

RESOLVE_PATH = Path(__file__).resolve().parent.parent / "daemon" / "resolve_threads.py"
_spec = importlib.util.spec_from_file_location("resolve_threads", RESOLVE_PATH)
assert _spec is not None and _spec.loader is not None
resolve_threads = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve_threads)

IncrementDiff = resolve_threads.IncrementDiff
select_candidates = resolve_threads.select_candidates
parse_verdict = resolve_threads.parse_verdict
MARKER = resolve_threads.PROVENANCE_MARKER


# ---------- IncrementDiff.parse / touched (OLD side) ----------


def test_old_side_single_hunk():
    diff = IncrementDiff.parse(
        textwrap.dedent(
            """\
            diff --git a/daemon/lib.sh b/daemon/lib.sh
            index abc..def 100644
            --- a/daemon/lib.sh
            +++ b/daemon/lib.sh
            @@ -10,3 +10,4 @@ foo()
                 x=1
            -    y=2
            +    y=3
            +    z=4
            """
        )
    )
    # Old side covers lines 10..12 (start 10, count 3).
    assert diff.touched("daemon/lib.sh", 10)
    assert diff.touched("daemon/lib.sh", 12)
    assert not diff.touched("daemon/lib.sh", 9)
    assert not diff.touched("daemon/lib.sh", 13)
    assert not diff.touched("other.sh", 10)


def test_pure_insertion_has_no_old_side_lines():
    # `@@ -41,0 +42,3 @@` is a pure insertion: nothing on the old side is touched.
    diff = IncrementDiff.parse(
        textwrap.dedent(
            """\
            diff --git a/f.py b/f.py
            @@ -41,0 +42,3 @@
            +a
            +b
            +c
            """
        )
    )
    assert diff.ranges["f.py"] == []
    assert not diff.touched("f.py", 41)
    assert not diff.touched("f.py", 42)


def test_old_count_defaults_to_one():
    diff = IncrementDiff.parse(
        textwrap.dedent(
            """\
            diff --git a/f.py b/f.py
            @@ -7 +7,2 @@
             keep
            +added
            """
        )
    )
    assert diff.touched("f.py", 7)
    assert not diff.touched("f.py", 8)


def test_rename_registers_both_paths():
    diff = IncrementDiff.parse(
        textwrap.dedent(
            """\
            diff --git a/old/name.py b/new/name.py
            @@ -3,2 +3,2 @@
            -old
            +new
            """
        )
    )
    assert diff.touched("old/name.py", 3)
    assert diff.touched("new/name.py", 3)


def test_touched_range_overlap():
    diff = IncrementDiff.parse(
        textwrap.dedent(
            """\
            diff --git a/f.py b/f.py
            @@ -20,2 +20,2 @@
            -a
            +b
            """
        )
    )
    # A multi-line finding [15..21] overlaps the touched range [20..21].
    assert diff.touched("f.py", 21, start_line=15)
    # A finding entirely above the touched range does not.
    assert not diff.touched("f.py", 18, start_line=15)


# ---------- select_candidates ----------


def _diff_touching(path: str, start: int, count: int) -> IncrementDiff:
    body = "".join(f"-line{i}\n" for i in range(count))
    return IncrementDiff.parse(
        f"diff --git a/{path} b/{path}\n@@ -{start},{count} +{start},0 @@\n{body}"
    )


def _thread(**over) -> dict:
    base = {
        "thread_id": "PRRT_1",
        "is_resolved": False,
        "root_author": "operator",
        "root_body": f"Unbounded loop.\n\n{MARKER}",
        "path": "daemon/lib.sh",
        "original_line": 10,
        "original_start_line": None,
    }
    base.update(over)
    return base


DIFF = _diff_touching("daemon/lib.sh", 10, 3)  # touches lines 10..12


def test_candidate_happy_path():
    out = select_candidates([_thread()], DIFF, "operator")
    assert out == [
        {
            "thread_id": "PRRT_1",
            "path": "daemon/lib.sh",
            "line": 10,
            "finding_body": f"Unbounded loop.\n\n{MARKER}",
        }
    ]


def test_resolved_thread_excluded():
    assert select_candidates([_thread(is_resolved=True)], DIFF, "operator") == []


def test_non_operator_author_excluded():
    assert select_candidates([_thread(root_author="someone-else")], DIFF, "operator") == []


def test_missing_provenance_marker_excluded():
    assert (
        select_candidates([_thread(root_body="A human's manual comment")], DIFF, "operator") == []
    )


def test_line_not_touched_excluded():
    assert select_candidates([_thread(original_line=99)], DIFF, "operator") == []


def test_null_original_line_excluded():
    assert select_candidates([_thread(original_line=None)], DIFF, "operator") == []


def test_all_open_bypasses_diff_filter():
    # original_line 99 is untouched, but all_open takes every open daemon thread.
    out = select_candidates([_thread(original_line=99)], DIFF, "operator", all_open=True)
    assert len(out) == 1 and out[0]["thread_id"] == "PRRT_1"


def test_all_open_still_enforces_open_and_ownership():
    threads = [
        _thread(thread_id="PRRT_resolved", is_resolved=True),
        _thread(thread_id="PRRT_human", root_author="someone-else"),
        _thread(thread_id="PRRT_keep", original_line=99),
    ]
    out = select_candidates(threads, DIFF, "operator", all_open=True)
    assert [c["thread_id"] for c in out] == ["PRRT_keep"]


# ---------- parse_verdict (safe-biased) ----------


def _fence(body: str) -> str:
    return f"Some reasoning here.\n\n```json\n{body}\n```\n"


def test_verdict_fixed_true():
    assert parse_verdict(_fence('{"fixed": true, "rationale": "loop now breaks on a cap"}')) == {
        "fixed": True,
        "rationale": "loop now breaks on a cap",
    }


def test_verdict_fixed_false():
    assert (
        parse_verdict(_fence('{"fixed": false, "rationale": "still one function"}'))["fixed"]
        is False
    )


def test_verdict_no_fence_leaves_open():
    assert parse_verdict("no fence at all")["fixed"] is False


def test_verdict_bad_json_leaves_open():
    assert parse_verdict(_fence("{not json}"))["fixed"] is False


def test_verdict_missing_fixed_leaves_open():
    assert parse_verdict(_fence('{"rationale": "x"}'))["fixed"] is False


def test_verdict_non_bool_fixed_leaves_open():
    assert parse_verdict(_fence('{"fixed": "yes", "rationale": "x"}'))["fixed"] is False


def test_verdict_empty_rationale_leaves_open():
    assert parse_verdict(_fence('{"fixed": true, "rationale": "  "}'))["fixed"] is False


def test_verdict_last_fence_wins():
    raw = _fence('{"fixed": true, "rationale": "first"}') + _fence(
        '{"fixed": false, "rationale": "second"}'
    )
    assert parse_verdict(raw) == {"fixed": False, "rationale": "second"}
