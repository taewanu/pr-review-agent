"""Tests for daemon/extract-json.py.

Slice 1: happy-path only. Slice 2 adds the edge-case coverage listed in
PRD #3's Testing Decisions (no-fence, malformed JSON, schema-invalid, etc.).

The module is loaded via importlib because the script file is hyphenated
(`extract-json.py`) — the hyphen blocks normal `import daemon.extract_json`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

EXTRACT_PATH = Path(__file__).resolve().parent.parent / "daemon" / "extract-json.py"
_spec = importlib.util.spec_from_file_location("extract_json", EXTRACT_PATH)
assert _spec is not None and _spec.loader is not None
extract_json = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract_json)


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
    assert finding.severity == "nit"
    assert finding.type == "polish"
    assert "parsed_payload" in finding.body
