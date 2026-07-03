"""Tests for daemon/lib.sh's resolve_tunable helper (ADR 0022).

The helper is a shell function, so tests source lib.sh and call it via `bash -c`.
It backs the CONFIDENCE_THRESHOLD dial: the exported environment wins, then the
KEY=VALUE line in .env, else empty (the caller supplies the default). The daemon
never sources .env wholesale, so this is the only path a value in .env reaches a
Python subprocess reading os.environ.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"


def _run(snippet: str, env: dict | None = None) -> str:
    # Run under the daemon's own flags: review-pr.sh sources lib.sh with
    # `set -euo pipefail`, so the helper must not abort when grep finds no
    # matching key (the default, key-absent case). A bare shell would mask
    # that regression.
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail; source {LIB}; {snippet}"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_env_wins_over_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("CONFIDENCE_THRESHOLD=70\n")
    import os

    env = {**os.environ, "CONFIDENCE_THRESHOLD": "90"}
    out = _run(f"resolve_tunable CONFIDENCE_THRESHOLD {dotenv}", env=env)
    assert out == "90"


def test_falls_back_to_dotenv_when_env_unset(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("FOO=bar\nCONFIDENCE_THRESHOLD=70\nBAZ=qux\n")
    import os

    env = {k: v for k, v in os.environ.items() if k != "CONFIDENCE_THRESHOLD"}
    out = _run(f"resolve_tunable CONFIDENCE_THRESHOLD {dotenv}", env=env)
    assert out == "70"


def test_empty_when_absent_everywhere(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("FOO=bar\n")
    import os

    env = {k: v for k, v in os.environ.items() if k != "CONFIDENCE_THRESHOLD"}
    out = _run(f"resolve_tunable CONFIDENCE_THRESHOLD {dotenv}", env=env)
    assert out == ""


def test_empty_when_dotenv_missing(tmp_path):
    import os

    env = {k: v for k, v in os.environ.items() if k != "CONFIDENCE_THRESHOLD"}
    missing = tmp_path / "nope.env"
    out = _run(f"resolve_tunable CONFIDENCE_THRESHOLD {missing}", env=env)
    assert out == ""


def test_strips_quotes_from_dotenv_value(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text('CONFIDENCE_THRESHOLD="65"\n')
    import os

    env = {k: v for k, v in os.environ.items() if k != "CONFIDENCE_THRESHOLD"}
    out = _run(f"resolve_tunable CONFIDENCE_THRESHOLD {dotenv}", env=env)
    assert out == "65"
