"""The Provenance tag is one string across every renderer (ADR 0010).

It is emitted from three sites that straddle the bash/Python boundary:
  - lib.sh        — the Status comment, via `PROVENANCE_TAG`
  - create-review.sh — the Inline comment, reusing lib.sh's `PROVENANCE_TAG`
  - create_reply.py — the reply, via its own `MARKER`

The bash sites share one constant (create-review.sh sources lib.sh), so the only
cross-language copy is create_reply.py's MARKER. This test pins both definitions to
the same canonical string, so a drift in either fails CI instead of shipping two
different tags.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_SH = REPO_ROOT / "daemon" / "lib.sh"
CREATE_REPLY = REPO_ROOT / "daemon" / "create_reply.py"

CANONICAL_TAG = "🤖 _pr-review-agent_"


def _lib_sh_provenance_tag() -> str:
    """Source lib.sh and print PROVENANCE_TAG, the bash-side single source."""
    out = subprocess.run(
        ["bash", "-c", f'source "{LIB_SH}" && printf "%s" "$PROVENANCE_TAG"'],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def _create_reply_marker() -> str:
    spec = importlib.util.spec_from_file_location("create_reply", CREATE_REPLY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MARKER


def test_lib_sh_tag_matches_canonical():
    assert _lib_sh_provenance_tag() == CANONICAL_TAG


def test_create_reply_marker_matches_canonical():
    assert _create_reply_marker() == CANONICAL_TAG


def test_bash_and_python_provenance_tags_agree():
    # The drift guard: the two independent definitions must stay identical.
    assert _lib_sh_provenance_tag() == _create_reply_marker()
