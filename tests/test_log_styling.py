"""Tests for the foreground output-styling helpers in daemon/lib.sh (#165).

The daemon log renders to two targets: a colour TTY (an operator watching a
foreground run) and a plaintext file. So colour and glyphs decorate, while the
repo#pr identity always lives in the line text. These tests pin that split:
colour is gated (PR_LOG_COLOR=never|always forces it off-TTY for the test), the
per-PR context swaps the [pr-review-agent] prefix for a coloured repo#pr one, the
quiet gate suppresses cycle-level chatter without ever hiding errors or per-PR
lines, and log_failure keeps its ADR 0005 machine-readable shape regardless. On a
terminal (a pseudo-terminal here) each line fits the width the terminal has when
the line prints; off one, every line keeps its full text.

lib.sh is sourced in a subprocess; helpers that build text print to stdout, the
log_* emitters print to stderr.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
import unicodedata
from pathlib import Path

from agent_fixture import clone_dir

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"

ESC = "\x1b"
SGR = re.compile(r"\x1b\[[0-9;]*m")


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
        {"type": "tool_use", "name": "Read",
         "input": {"file_path": "src/" + "nested/" * 15 + "a.py"}}]}},
    {"type": "result", "result": "partial", "num_turns": 2, "total_cost_usd": 0.5},
    {"type": "assistant", "message": {"content": [{"type": "text",
        "text": "all done; " + "nothing in the changed files is worth flagging " * 3}]}},
    {"type": "result", "result": "final", "num_turns": 1, "total_cost_usd": 0.5},
):
    print(json.dumps(event), flush=True)
"""


def _set_columns(fd: int, columns: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))


def _width(line: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in line)


def _on_terminal(
    argv: list[str],
    columns: int,
    env: dict[str, str],
    resize: tuple[int, Path] | None = None,
) -> list[str]:
    """Run `argv` with stderr on a pseudo-terminal `columns` wide; its stderr lines
    with the colour codes stripped. With `resize` (columns, flag), the terminal
    takes the new width once the first line arrives, and then the flag file appears."""
    master, slave = pty.openpty()
    _set_columns(slave, columns)
    deadline = time.monotonic() + 60
    with subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=slave, env=env
    ) as proc:
        os.close(slave)
        out = b""
        while True:
            ready, _, _ = select.select([master], [], [], max(deadline - time.monotonic(), 0))
            if not ready:
                proc.kill()
                os.close(master)
                raise AssertionError(f"no end of output within 60s: {out!r}")
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if resize and b"\n" in out:
                _set_columns(master, resize[0])
                resize[1].touch()
                resize = None
    os.close(master)
    assert proc.returncode == 0, out
    return SGR.sub("", out.decode().replace("\r\n", "\n")).splitlines()


def _pr_run(
    tmp_path: Path,
    colour: str,
    columns: int | None = None,
    message: str = "head: example/example@main",
) -> list[str]:
    """One PR's lines: every per-PR log_* emitter around one run_agent call, with
    stderr on a terminal `columns` wide when given."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").write_text(_FAKE_CLAUDE)
    (bindir / "claude").chmod(0o755)
    clone = clone_dir(tmp_path)
    clone.mkdir(parents=True)
    (clone / "prompt.txt").write_text("prompt\n")
    script = (
        "set -euo pipefail; log_set_pr_context example 7; "
        'REVIEW_MODEL=m; REVIEW_EFFORT=e; cd "$1"; '
        'log_info "$2"; log_step "fetching diff"; '
        "run_agent read-only 30 prompt.txt raw.txt review; "
        'log_ok "submitted review"; log_err "status comment edit failed"'
    )
    argv = ["bash", "-c", f"source {LIB}\n{script}", "_", str(clone), message]
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "PR_LOG_COLOR": colour,
    }
    if columns is not None:
        return _on_terminal(argv, columns, env)
    result = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
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


def test_off_a_terminal_lines_keep_their_full_text():
    long = "no new findings at " + "0" * 40 + "; skipping review object (ADR 0020)"
    _, err, _ = _run(
        f'SECONDS=5; log_step "polling {"r" * 90}"; log_set_pr_context example 7; '
        f'log_info "fetching diff"; log_info "{long}"',
        PR_LOG_COLOR="never",
    )
    assert err == (
        f"[pr-review-agent] polling {'r' * 90} (+5s)\n"
        f"  example#7  • fetching diff{' ' * 40}+5s\n"
        f"  example#7  • {long}     +5s\n"
    )


# --- terminal width ------------------------------------------------------------

LONG_MESSAGE = "no new findings at " + "0" * 40 + "; skipping review object (ADR 0020)"


def test_on_a_terminal_every_line_of_a_pr_run_fits_its_width(tmp_path):
    for columns in (60, 80, 140):
        run_dir = tmp_path / str(columns)
        run_dir.mkdir()
        lines = _pr_run(run_dir, "auto", columns, LONG_MESSAGE)
        assert len(lines) == 7, lines
        assert all(len(line) <= columns for line in lines), (columns, lines)
        assert any("…" in line for line in lines), lines


def test_on_a_terminal_a_pipeline_line_ends_with_its_elapsed_time_at_the_right_edge(tmp_path):
    lines = [
        line for line in _pr_run(tmp_path, "auto", 80, LONG_MESSAGE) if " review  " not in line
    ]
    assert all(re.search(r" \+\d+s$", line) and len(line) == 80 for line in lines), lines
    assert lines[0].startswith("  example#7  • no new findings at 000")


def test_on_a_terminal_every_line_of_a_pr_run_starts_its_message_in_one_column(tmp_path):
    lines = _pr_run(tmp_path, "auto", 60, LONG_MESSAGE)
    words = ["no", "fetching", "review", "review", "review", "submitted", "status"]
    assert len({line.index(word) for line, word in zip(lines, words, strict=True)}) == 1, lines


def test_on_a_terminal_a_path_keeps_its_file_name(tmp_path):
    tool = [line for line in _pr_run(tmp_path, "auto", 60) if "→ Read" in line]
    assert len(tool) == 1 and tool[0].endswith("/a.py") and "…" in tool[0], tool


def test_on_a_terminal_a_failure_line_keeps_its_whole_reason():
    reason = "run_agent: refusing to run codex here; set TMPDIR to a private directory"
    lines = _on_terminal(
        ["bash", "-c", f'source {LIB}\nlog_set_pr_context example 7; log_err "{reason}"'],
        40,
        dict(os.environ),
    )
    assert reason in "".join(lines)


def test_on_a_terminal_a_cycle_line_fits_and_keeps_its_elapsed_time():
    lines = _on_terminal(
        ["bash", "-c", f'source {LIB}\nSECONDS=5; log_step "polling {"r" * 90}"'],
        60,
        dict(os.environ),
    )
    assert lines == [f"[pr-review-agent] polling {'r' * 27}… (+5s)"]


def test_a_terminal_narrower_than_the_floor_still_shows_twenty_columns_of_text():
    lines = _on_terminal(
        ["bash", "-c", f'source {LIB}\nlog_set_pr_context example 7; log_info "{"x" * 90}"'],
        10,
        dict(os.environ),
    )
    assert f"• {'x' * 19}…  +" in "".join(lines)


def test_on_a_terminal_codex_activity_lines_fit_its_width(tmp_path):
    events = tmp_path / "events.ndjson"
    events.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "command": "grep -rn " + "x" * 120},
                },
                {"type": "item.completed", "item": {"type": "agent_message", "text": "y" * 120}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "검토" * 40}},
                {"type": "error", "message": "z" * 120},
                {"type": "turn.completed", "usage": {"input_tokens": 1000, "output_tokens": 5}},
            )
        )
        + "\n"
    )
    gone = subprocess.Popen(["true"])
    gone.wait()
    prefix = "  \x1b[36mexample#7\x1b[0m    review     "
    lines = _on_terminal(
        [
            sys.executable,
            str(REPO_ROOT / "daemon" / "codex_exec.py"),
            "format",
            "--events",
            str(events),
            "--while-pid",
            str(gone.pid),
            "--prefix",
            prefix,
            "--tokens-out",
            str(tmp_path / "tokens"),
        ],
        60,
        dict(os.environ),
    )
    assert len(lines) == 5, lines
    assert [_width(line) for line in lines[:4]] == [60, 60, 59, 60], lines
    assert lines[2].endswith("검토…"), lines
    assert all(line.startswith("  example#7    review     ") for line in lines), lines


def test_on_a_terminal_each_line_reads_the_width_it_prints_at(tmp_path):
    flag = tmp_path / "resized"
    script = (
        f'log_set_pr_context example 7; log_info "{"x" * 90}"; '
        f'until [[ -e "{flag}" ]]; do sleep 0.05; done; log_info "{"x" * 90}"'
    )
    lines = _on_terminal(
        ["bash", "-c", f"source {LIB}\n{script}"], 80, dict(os.environ), resize=(50, flag)
    )
    assert [len(line) for line in lines] == [80, 50], lines


def test_on_a_terminal_a_degradation_warning_keeps_its_whole_text(tmp_path):
    warning = "merge-skip: lens data-flow skipped (ValueError: " + "w" * 60 + ")"
    stderr_file = tmp_path / "merge.stderr"
    stderr_file.write_text(warning + "\n")
    lines = _on_terminal(
        [
            "bash",
            "-c",
            f'source {LIB}\nlog_set_pr_context example 7; log_degradation_warnings "{stderr_file}"',
        ],
        60,
        dict(os.environ),
    )
    assert warning in "".join(lines)
