"""Tests for daemon/index_findings.py — stamp a 0-based index per finding (#258)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

INDEX_PATH = Path(__file__).resolve().parent.parent / "daemon" / "index_findings.py"
_spec = importlib.util.spec_from_file_location("index_findings", INDEX_PATH)
assert _spec is not None and _spec.loader is not None
index_findings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(index_findings)


def _payload(*paths: str) -> dict:
    return {"summary": "s", "comments": [{"path": p, "line": 1, "body": "b"} for p in paths]}


def test_index_matches_array_position():
    result = index_findings.index_findings(_payload("a.py", "b.py", "c.py"))
    assert [c["index"] for c in result["comments"]] == [0, 1, 2]


def test_existing_fields_are_preserved():
    result = index_findings.index_findings(_payload("a.py"))
    assert result["comments"][0] == {"index": 0, "path": "a.py", "line": 1, "body": "b"}
    assert result["summary"] == "s"


def test_input_is_not_mutated():
    payload = _payload("a.py")
    index_findings.index_findings(payload)
    # apply_edits reads the same author file; a leaked index there would post.
    assert "index" not in payload["comments"][0]


def test_no_comments_is_a_no_op():
    assert index_findings.index_findings({"summary": "s", "comments": []})["comments"] == []
