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
import re
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


# --- one PR's run: pipeline lines and agent activity --------------------------

_FAKE_CLAUDE = """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
for event in (
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}},
    {"type": "result", "result": "partial", "num_turns": 2, "total_cost_usd": 0.5},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "all done"}]}},
    {"type": "result", "result": "final", "num_turns": 1, "total_cost_usd": 0.5},
):
    print(json.dumps(event), flush=True)
"""


def _pr_run(tmp_path: Path, colour: str) -> list[str]:
    """One PR's lines: every per-PR log_* emitter around one run_agent call."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").write_text(_FAKE_CLAUDE)
    (bindir / "claude").chmod(0o755)
    (tmp_path / "prompt.txt").write_text("prompt\n")
    script = (
        "set -euo pipefail; log_set_pr_context example 7; "
        'REVIEW_MODEL=m; REVIEW_EFFORT=e; cd "$1"; '
        'log_info "head: example/example@main"; log_step "fetching diff"; '
        "run_agent read-only 30 prompt.txt raw.txt review; "
        'log_ok "submitted review"; log_err "status comment edit failed"'
    )
    result = subprocess.run(
        ["bash", "-c", f"source {LIB}\n{script}", "_", str(tmp_path)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "PR_LOG_COLOR": colour,
        },
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stderr.splitlines()


def test_every_line_of_a_pr_run_starts_its_message_in_one_column(tmp_path):
    lines = _pr_run(tmp_path, "never")
    words = ["head:", "fetching", "review", "review", "review", "submitted", "status"]
    assert len(lines) == len(words), lines
    assert all(line.startswith("  example#7  ") for line in lines)
    assert len({line.index(word) for line, word in zip(lines, words, strict=True)}) == 1, lines


def test_an_agent_call_closes_with_one_line(tmp_path):
    lines = _pr_run(tmp_path, "never")
    closing = [line for line in lines if "done ·" in line]
    assert len(closing) == 1, lines
    assert re.search(r" review +done · 3 turns · \d+s · \$0\.500$", closing[0]), closing


def test_pipeline_lines_align_their_elapsed_time(tmp_path):
    lines = [line for line in _pr_run(tmp_path, "never") if " review  " not in line]
    assert all(re.search(r" \+\d+s$", line) for line in lines), lines
    assert len({len(line) for line in lines}) == 1, lines


def test_a_long_message_still_carries_its_elapsed_time():
    _, err, _ = _run(f'log_set_pr_context example 7; log_info "{"x" * 120}"', PR_LOG_COLOR="never")
    assert re.search(r"x +\+\d+s$", err.rstrip("\n"))


def test_a_multi_line_message_puts_its_elapsed_time_after_the_last_line_unpadded():
    _, err, _ = _run(
        "log_set_pr_context example 7; log_info \"$(printf 'first\\nsecond')\"",
        PR_LOG_COLOR="never",
    )
    assert re.fullmatch(r"  example#7  • first\nsecond {4,5}\+\d+s\n", err), err


def test_non_terminal_output_carries_no_colour_codes(tmp_path):
    # stderr is a pipe here, so the auto mode must fall back to plain text.
    lines = _pr_run(tmp_path, "auto")
    assert lines
    assert not any(ESC in line for line in lines)
