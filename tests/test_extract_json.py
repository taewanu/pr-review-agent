"""Tests for daemon/extract-json.py."""

from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

# Script filename is hyphenated, which blocks `import daemon.extract_json`.
EXTRACT_PATH = Path(__file__).resolve().parent.parent / "daemon" / "extract-json.py"
_spec = importlib.util.spec_from_file_location("extract_json", EXTRACT_PATH)
assert _spec is not None and _spec.loader is not None
extract_json = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract_json)


def _wrap(payload: dict) -> str:
    return f"prose before\n\n```json\n{json.dumps(payload)}\n```\n"


def _minimal_finding(**overrides) -> dict:
    finding = {
        "path": "src/main.py",
        "line": 42,
        "severity": "nit",
        "type": "polish",
        "body": "small naming nit",
    }
    finding.update(overrides)
    return finding


def test_happy_path_returns_review_payload():
    raw = """\
Thinking out loud about the diff here...

```json
{
  "summary": "Solid diff. One naming nit worth flagging before merge.",
  "comments": [
    {
      "path": "src/main.py",
      "line": 42,
      "severity": "nit",
      "type": "polish",
      "body": "`tmp` reads as throwaway — `parsed_payload` would carry the intent."
    }
  ]
}
```
"""
    payload = extract_json.extract(raw)
    assert payload.summary.startswith("Solid diff")
    assert len(payload.comments) == 1
    finding = payload.comments[0]
    assert finding.path == "src/main.py"
    assert finding.line == 42
    assert finding.end_line is None
    assert finding.severity == "nit"
    assert finding.type == "polish"
    assert "parsed_payload" in finding.body


def test_no_fence_raises():
    with pytest.raises(ValueError, match="no ```json fence"):
        extract_json.extract("just some prose, no fence here at all\n")


def test_multiple_fences_picks_last():
    raw = textwrap.dedent(
        """\
        Considering option A:

        ```json
        {"summary": "first draft", "comments": []}
        ```

        Actually, going with option B:

        ```json
        {"summary": "final draft", "comments": []}
        ```
        """
    )
    payload = extract_json.extract(raw)
    assert payload.summary == "final draft"


def test_malformed_json_inside_fence_raises():
    raw = "```json\n{not valid json at all\n```\n"
    with pytest.raises(ValidationError):
        extract_json.extract(raw)


def test_missing_required_field_raises():
    raw = _wrap({"summary": "missing comments field"})
    with pytest.raises(ValidationError):
        extract_json.extract(raw)


def test_invalid_enum_value_raises():
    bad = _minimal_finding(severity="critical")  # not in {important, nit, pre_existing}
    raw = _wrap({"summary": "x", "comments": [bad]})
    with pytest.raises(ValidationError):
        extract_json.extract(raw)


def test_empty_comments_is_valid():
    raw = _wrap({"summary": "nothing to flag", "comments": []})
    payload = extract_json.extract(raw)
    assert payload.summary == "nothing to flag"
    assert payload.comments == []


def test_cap_exceeded_raises():
    comments = [_minimal_finding(line=i) for i in range(1, extract_json.MAX_FINDINGS + 2)]
    raw = _wrap({"summary": "lots", "comments": comments})
    with pytest.raises(ValueError, match="too many findings"):
        extract_json.extract(raw)


def test_end_line_equal_to_line_is_valid():
    f = _minimal_finding(line=10, end_line=10)
    raw = _wrap({"summary": "x", "comments": [f]})
    payload = extract_json.extract(raw)
    assert payload.comments[0].end_line == 10


def test_end_line_greater_than_line_is_valid():
    f = _minimal_finding(line=10, end_line=20)
    raw = _wrap({"summary": "x", "comments": [f]})
    payload = extract_json.extract(raw)
    assert payload.comments[0].end_line == 20


def test_end_line_less_than_line_raises():
    f = _minimal_finding(line=20, end_line=10)
    raw = _wrap({"summary": "x", "comments": [f]})
    with pytest.raises(ValidationError, match="end_line"):
        extract_json.extract(raw)
