"""Tests for daemon/diff_paths.py's `---`/`+++` header path parsing (#13).

The forms below are what `git diff` actually emits, verified against real
output: a plain path, a space-bearing path terminated by a tab, and a path
holding a byte git escapes wrapped in double quotes with C-style escapes
(octal `\\ooo` for the UTF-8 bytes of a non-ASCII character).
"""

from __future__ import annotations

import importlib.util
import subprocess
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


# --- paths_in_diff and the CLI (#306) --------------------------------------
#
# These replace two tests that exercised a `diff_paths` helper in lib.sh. That
# helper matched the `diff --git` header and so dropped exactly the paths this
# module exists to keep; the cases below carry the old coverage plus the one
# that broke it.


def _cli(tmp_path, text: str) -> tuple[str, int]:
    diff_file = tmp_path / "d.diff"
    diff_file.write_text(text)
    result = subprocess.run(
        ["python3", str(DIFF_PATHS), str(diff_file)], capture_output=True, text=True
    )
    return result.stdout, result.returncode


def test_paths_in_diff_lists_every_file_in_order(tmp_path):
    diff = (
        "diff --git a/daemon/poll.sh b/daemon/poll.sh\n"
        "index 111..222 100644\n"
        "--- a/daemon/poll.sh\n+++ b/daemon/poll.sh\n"
        "@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n+++ b/README.md\n"
    )
    out, rc = _cli(tmp_path, diff)
    assert rc == 0
    assert out.splitlines() == ["daemon/poll.sh", "README.md"]


def test_cli_prints_nothing_for_a_missing_file(tmp_path):
    result = subprocess.run(
        ["python3", str(DIFF_PATHS), str(tmp_path / "nope")], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_a_quoted_path_survives(tmp_path):
    # The case the shell helper dropped: git C-quotes a non-ASCII path, and a
    # pattern anchored on `diff --git a/… b/…` matches nothing.
    diff = (
        'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
        '--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n'
        "@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
    )
    out, rc = _cli(tmp_path, diff)
    assert rc == 0
    assert out.splitlines() == ["café.py", "README.md"]


def test_a_deleted_file_yields_its_path(tmp_path):
    diff = (
        "diff --git a/daemon/gone.py b/daemon/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/daemon/gone.py\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-old\n"
    )
    out, rc = _cli(tmp_path, diff)
    assert rc == 0
    assert out.splitlines() == ["daemon/gone.py"]


# --- renames: one file, two names -------------------------------------------
#
# The two shapes below are verbatim `git diff` output. They are why the file
# list and the routing classifier need different answers: a list a human reads
# wants one entry under the surviving name, while a classifier asking what the
# diff touched has to see both, since a rename from .py to .md removed code.

_RENAME_WITH_EDITS = (
    "diff --git a/old.py b/new.py\n"
    "similarity index 50%\n"
    "rename from old.py\n"
    "rename to new.py\n"
    "index de98044..70540de 100644\n"
    "--- a/old.py\n"
    "+++ b/new.py\n"
    "@@ -1,3 +1,3 @@\n a\n b\n-c\n+ZZZ\n"
)

_PURE_RENAME = "diff --git a/n.py b/f.py\nsimilarity index 100%\nrename from n.py\nrename to f.py\n"


def test_changed_files_counts_a_renamed_file_once():
    assert diff_paths.changed_files(_RENAME_WITH_EDITS) == ["new.py"]


def test_changed_files_keeps_a_pure_rename():
    # No `---`/`+++` pair exists here, so a parser reading only those loses the
    # file entirely and reports a count of zero.
    assert diff_paths.changed_files(_PURE_RENAME) == ["f.py"]


def test_changed_files_names_a_deletion_by_its_only_name():
    diff = (
        "diff --git a/daemon/gone.py b/daemon/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/daemon/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
    )
    assert diff_paths.changed_files(diff) == ["daemon/gone.py"]


def test_paths_in_diff_keeps_both_names_of_a_rename():
    assert diff_paths.paths_in_diff(_RENAME_WITH_EDITS) == ["old.py", "new.py"]
    assert diff_paths.paths_in_diff(_PURE_RENAME) == ["n.py", "f.py"]


def test_cli_counts_a_renamed_file_once(tmp_path):
    out, rc = _cli(tmp_path, _RENAME_WITH_EDITS + _PURE_RENAME)
    assert rc == 0
    assert out.splitlines() == ["new.py", "f.py"]
