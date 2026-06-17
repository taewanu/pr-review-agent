"""Tests for the foreground output-styling helpers in daemon/lib.sh (#165).

The daemon log renders to two targets: a colour TTY (an operator watching a
foreground run) and a plaintext file. So colour and glyphs decorate, while the
repo#pr identity always lives in the line text. These tests pin that split:
colour is gated (PR_LOG_COLOR=never|always forces it off-TTY for the test), the
per-PR context swaps the [pr-review-agent] prefix for a coloured repo#pr one, the
quiet gate suppresses cycle-level chatter without ever hiding errors or per-PR
lines, and log_failure keeps its ADR 0005 machine-readable shape regardless.

lib.sh is sourced in a subprocess; helpers that build text print to stdout, the
log_* emitters print to stderr.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"

ESC = "\x1b"


def _run(script: str, **env_extra: str) -> tuple[str, str, int]:
    """Source lib.sh and run `script`. Returns (stdout, stderr, returncode)."""
    env = os.environ.copy()
    env.update(env_extra)
    result = subprocess.run(
        ["bash", "-c", f"source {LIB}\n{script}"],
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout, result.stderr, result.returncode


# --- pr_color_index ------------------------------------------------------------


def test_color_index_is_in_palette_range():
    out, _, rc = _run(
        'for k in a bb "pr-review-agent#162" "sounds-abroad#76" "" z; do '
        'pr_color_index "$k"; echo; done'
    )
    assert rc == 0
    indices = [int(x) for x in out.split()]
    assert indices, "expected one index per key"
    assert all(0 <= i < 8 for i in indices)


def test_color_index_is_stable_for_a_key():
    a, _, _ = _run('pr_color_index "pr-review-agent#162"')
    b, _, _ = _run('pr_color_index "pr-review-agent#162"')
    assert a == b


# --- colour gate ---------------------------------------------------------------


def test_prefix_is_plain_text_when_colour_off():
    out, _, rc = _run("pr_prefix pr-review-agent 162", PR_LOG_COLOR="never")
    assert rc == 0
    assert out == "pr-review-agent#162"
    assert ESC not in out


def test_prefix_wraps_in_sgr_when_colour_on():
    out, _, _ = _run("pr_prefix pr-review-agent 162", PR_LOG_COLOR="always")
    assert "pr-review-agent#162" in out
    assert ESC in out


# --- per-PR context ------------------------------------------------------------


def test_log_info_uses_plain_prefix_without_context():
    _, err, _ = _run('log_info "hello"', PR_LOG_COLOR="never")
    assert err.strip() == "[pr-review-agent] hello"


def test_log_info_uses_repo_pr_prefix_with_context():
    _, err, _ = _run(
        'log_set_pr_context pr-review-agent 162; log_info "fetching diff"',
        PR_LOG_COLOR="never",
    )
    assert "[pr-review-agent]" not in err
    assert "pr-review-agent#162" in err
    assert "fetching diff" in err


# --- quiet gate ----------------------------------------------------------------


def test_quiet_suppresses_cycle_lines_but_not_errors():
    _, err, _ = _run(
        '_LOG_QUIET=1; log_info "noise"; log_step "more noise"; log_err "real problem"',
        PR_LOG_COLOR="never",
    )
    assert "noise" not in err
    assert err.strip() == "[pr-review-agent] ERROR: real problem"


def test_quiet_does_not_suppress_per_pr_lines():
    _, err, _ = _run(
        '_LOG_QUIET=1; log_set_pr_context r 1; log_info "kept"',
        PR_LOG_COLOR="never",
    )
    assert "kept" in err


# --- ADR 0005 scraper contract -------------------------------------------------


def test_log_failure_stays_machine_readable_under_context_and_colour():
    _, err, _ = _run(
        "log_set_pr_context r 1; "
        'log_failure review-timeout https://x/pull/1 deadbeef "agent exceeded cap"',
        PR_LOG_COLOR="always",
    )
    assert (
        "[pr-review-agent] failure: review-timeout "
        "pr=https://x/pull/1 sha=deadbeef reason=agent exceeded cap" in err
    )
    assert ESC not in err
