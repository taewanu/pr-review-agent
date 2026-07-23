"""Tests for daemon/diff_paths.py's `---`/`+++` header path parsing (#13).

The forms below are what `git diff` actually emits, verified against real
output: a plain path, a space-bearing path terminated by a tab, and a path
holding a byte git escapes wrapped in double quotes with C-style escapes
(octal `\\ooo` for the UTF-8 bytes of a non-ASCII character).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

DIFF_PATHS = Path(__file__).resolve().parent.parent / "daemon" / "diff_paths.py"
_spec = importlib.util.spec_from_file_location("diff_paths", DIFF_PATHS)
assert _spec is not None and _spec.loader is not None
diff_paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diff_paths)
parse_diff_path = diff_paths.parse_diff_path


def test_plain_new_path():
    assert parse_diff_path("+++ b/src/foo.py", "b/") == "src/foo.py"


def test_plain_old_path():
    assert parse_diff_path("--- a/src/foo.py", "a/") == "src/foo.py"


def test_space_path_is_tab_terminated():
    # git appends a tab after a path that holds a space; it is not the path.
    assert parse_diff_path("+++ b/dir/with space.py\t", "b/") == "dir/with space.py"


def test_space_old_path_with_trailing_timestamp():
    assert parse_diff_path("--- a/with space.py\t2024-01-01", "a/") == "with space.py"


def test_path_containing_b_slash_substring():
    # The `diff --git a/… b/…` line cannot be split here; the `+++` line can.
    assert parse_diff_path("+++ b/x b/y.py\t", "b/") == "x b/y.py"


def test_quoted_non_ascii_path_is_unescaped():
    # `wît nonascii.py`: the î is octal-escaped as its two UTF-8 bytes.
    assert parse_diff_path(r'+++ "b/w\303\256t nonascii.py"', "b/") == "wît nonascii.py"


def test_quoted_escaped_quote_and_backslash():
    assert parse_diff_path(r'+++ "b/a\"q\\r.py"', "b/") == 'a"q\\r.py'


def test_quoted_tab_escape():
    assert parse_diff_path(r'+++ "b/a\tb.py"', "b/") == "a\tb.py"


def test_dev_null_is_none():
    assert parse_diff_path("+++ /dev/null", "b/") is None
    assert parse_diff_path("--- /dev/null", "a/") is None


def test_wrong_marker_side_returns_none():
    assert parse_diff_path("+++ b/foo.py", "a/") is None
    assert parse_diff_path("--- a/foo.py", "b/") is None


def test_unterminated_quote_is_none():
    assert parse_diff_path('+++ "b/oops.py', "b/") is None


def test_malformed_octal_escape_is_none_not_raise():
    # git always emits three octal digits; a short or oversized one is malformed
    # and must degrade to None rather than raise out of the parser.
    assert parse_diff_path(r'+++ "b/a\7"', "b/") is None
    assert parse_diff_path(r'+++ "b/a\777b.py"', "b/") is None
