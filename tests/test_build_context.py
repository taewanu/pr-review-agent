"""Tests for daemon/build_context.py (ADR 0029, shared context pack).

Covers: definition-only symbol extraction across Python and bash with the
short-name and too-common filters, added-line parsing from a diff (only `+`
lines, multi-file), the reference grep (word-boundary, per-symbol cap, changed
files excluded), related-test discovery, and the assembled-pack sections."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

BUILD_PATH = Path(__file__).resolve().parent.parent / "daemon" / "build_context.py"
_spec = importlib.util.spec_from_file_location("build_context", BUILD_PATH)
assert _spec is not None and _spec.loader is not None
build_context = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_context)


# --- extract_symbols -------------------------------------------------------


def test_extract_symbols_python_def_and_class():
    added = {"foo.py": ["def parse_status(raw):", "class WidgetBuilder:", "    return raw"]}
    assert build_context.extract_symbols(added) == {"parse_status", "WidgetBuilder"}


def test_extract_symbols_async_def():
    added = {"a.py": ["async def fetch_thing(url):"]}
    assert build_context.extract_symbols(added) == {"fetch_thing"}


def test_extract_symbols_bash_functions():
    added = {"run.sh": ["acquire_slot() {", "function release_slot {"]}
    assert build_context.extract_symbols(added) == {"acquire_slot", "release_slot"}


def test_extract_symbols_ignores_calls_and_assignments():
    # A call site, an assignment, and a comment mentioning a name are not
    # definitions, so none seed the reference map.
    added = {"x.py": ["result = parse_status(raw)", "TOTAL = 5", "# parse_status is nice"]}
    assert build_context.extract_symbols(added) == set()


def test_extract_symbols_filters_short_and_common():
    added = {"x.py": ["def id(x):", "def run(x):", "class Ab:", "def handler(e):"]}
    # `id`/`Ab` too short (<3), `run`/`handler` too common.
    assert build_context.extract_symbols(added) == set()


def test_extract_symbols_indented_def_matches():
    added = {"x.py": ["    def method_body(self):"]}
    assert build_context.extract_symbols(added) == {"method_body"}


# --- changed_added_lines ---------------------------------------------------

_DIFF = """diff --git a/daemon/foo.py b/daemon/foo.py
index 111..222 100644
--- a/daemon/foo.py
+++ b/daemon/foo.py
@@ -1,2 +1,4 @@
 import os
+def parse_status(raw):
+    return raw.strip()
diff --git a/bar.sh b/bar.sh
index 333..444 100644
--- a/bar.sh
+++ b/bar.sh
@@ -1 +1,2 @@
 set -e
+acquire_slot() {
"""


def test_changed_added_lines_only_added_per_file():
    by_path = build_context.changed_added_lines(_DIFF)
    assert set(by_path) == {"daemon/foo.py", "bar.sh"}
    # Context line `import os` and header lines excluded; only `+` bodies kept,
    # marker stripped.
    assert by_path["daemon/foo.py"] == ["def parse_status(raw):", "    return raw.strip()"]
    assert by_path["bar.sh"] == ["acquire_slot() {"]


def test_changed_added_lines_end_to_end_symbols():
    by_path = build_context.changed_added_lines(_DIFF)
    assert build_context.extract_symbols(by_path) == {"parse_status", "acquire_slot"}


# --- find_references -------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _stage(root: Path) -> None:
    """Init a git repo and stage every file, so _tracked_files (git ls-files)
    sees the fixtures. build_context restricts the reference grep to tracked
    files on purpose (an untracked secret must not leak into the pack), so the
    fixtures have to be tracked to be searched."""
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)


def test_find_references_word_boundary_and_excludes_changed(tmp_path):
    _write(tmp_path, "caller.py", "x = parse_status(y)\n")
    _write(tmp_path, "other.py", "reparse_status_helper()\n")  # not a word-boundary hit
    _write(tmp_path, "foo.py", "def parse_status(raw): ...\n")  # the changed file itself
    _stage(tmp_path)
    refs = build_context.find_references({"parse_status"}, tmp_path, {"foo.py"})
    hits = refs.get("parse_status", [])
    assert any("caller.py" in h for h in hits)
    assert not any("other.py" in h for h in hits)  # substring, not a reference
    assert not any("foo.py" in h for h in hits)  # changed file skipped


def test_find_references_caps_per_symbol(tmp_path):
    body = "\n".join(f"call_{i}(); use_widget()" for i in range(50))
    _write(tmp_path, "big.py", body)
    _stage(tmp_path)
    refs = build_context.find_references({"use_widget"}, tmp_path, set())
    assert len(refs["use_widget"]) == build_context._MAX_REFS_PER_SYMBOL


# --- find_related_tests ----------------------------------------------------


def test_find_related_tests_matches_basename_or_symbol(tmp_path):
    _write(tmp_path, "tests/test_foo.py", "from foo import parse_status\n")
    _write(tmp_path, "tests/test_unrelated.py", "assert 1 == 1\n")
    _write(tmp_path, "src/foo.py", "def parse_status(): ...\n")  # not a test file
    _stage(tmp_path)
    tests = build_context.find_related_tests({"src/foo.py"}, {"parse_status"}, tmp_path)
    assert "tests/test_foo.py" in tests
    assert "tests/test_unrelated.py" not in tests
    assert "src/foo.py" not in tests  # not under a test path


# --- build_pack ------------------------------------------------------------


def test_build_pack_has_all_sections(tmp_path):
    _write(tmp_path, "daemon/foo.py", "def parse_status(raw): ...\n")
    _write(tmp_path, "caller.py", "parse_status(1)\n")
    _write(tmp_path, "tests/test_foo.py", "import foo\n")
    _write(tmp_path, "docs/adr/0001-x.md", "# ADR 0001: A baseline\n")
    _stage(tmp_path)
    pack = build_context.build_pack(_DIFF, tmp_path)
    assert "## Changed symbols" in pack
    assert "parse_status" in pack
    assert "## References to changed symbols" in pack
    assert "caller.py" in pack
    assert "## Related tests" in pack
    assert "ADR 0001: A baseline" in pack
