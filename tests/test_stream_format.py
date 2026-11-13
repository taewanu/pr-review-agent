"""Tests for daemon/stream_format.py, the live `claude -p` progress view (#176).

The formatter sits in the pipe between the agent and the parser. Two contracts
matter and are pinned here:

  1. Reconstruction is faithful. The terminal `result` event's `result` field is
     written verbatim to --raw-out, so extract_json.py / resolve_threads.py /
     create_reply.py read the same bytes text-mode gave them. A truncated stream
     (no result event, e.g. a killed-on-timeout agent) leaves --raw-out empty, so
     the call site's `[[ -s ]]` check still fires.
  2. Exit-code discipline. The call sites pipe under `set -o pipefail`, so the
     pipeline can only surface claude's own status (142 on the timeout backstop) if
     the formatter exits 0. The shell test below pins that end to end.

The render layer is editorial, not load-bearing: tool actions and the agent's prose
get a line, reasoning/hook/rate-limit noise is dropped, and a stream that holds a
result closes with one summary line; one that holds none (a timeout or a crash)
gets no closing line, and the caller's own line reports the exit.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "daemon"))

import stream_format as sf  # noqa: E402

TIMEOUT_EXIT = 142


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


# --- render_event: which events earn a line ---------------------------------


def test_tool_use_renders_name_and_key_arg():
    line = sf.render_event(
        _assistant({"type": "tool_use", "name": "Read", "input": {"file_path": "a/b.py"}})
    )
    assert line == "→ Read a/b.py"


def test_result_event_renders_no_line_of_its_own():
    # One call can emit several result events; run() closes the call once.
    assert sf.render_event({"type": "result", "num_turns": 3, "total_cost_usd": 0.19}) is None


def test_closing_line_counts_turns_in_the_singular():
    assert sf.closing_line(1, 8.2, 0.1914) == "done · 1 turn · 8s · $0.191"
    assert sf.closing_line(3, 8.2, 0.1914) == "done · 3 turns · 8s · $0.191"


def test_closing_line_omits_fields_the_stream_did_not_report():
    assert sf.closing_line(None, 8.2, None) == "done · 8s"


def test_text_block_is_shown_truncated():
    line = sf.render_event(_assistant({"type": "text", "text": "  reviewing the diff  "}))
    assert line == "reviewing the diff"


def test_thinking_block_is_skipped():
    assert sf.render_event(_assistant({"type": "thinking", "thinking": "secret reasoning"})) is None


def test_empty_text_block_is_skipped():
    assert sf.render_event(_assistant({"type": "text", "text": "   "})) is None


def test_noise_event_types_are_skipped():
    for evt in (
        {"type": "system", "subtype": "init"},
        {"type": "system", "subtype": "hook_started"},
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {"type": "rate_limit_event"},
    ):
        assert sf.render_event(evt) is None


def test_multiple_blocks_join_one_per_line():
    line = sf.render_event(
        _assistant(
            {"type": "thinking", "thinking": "x"},
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "TODO"}},
            {"type": "text", "text": "found it"},
        )
    )
    assert line == "→ Grep TODO\nfound it"


def test_multi_line_prose_keeps_a_separator_between_lines():
    line = sf.render_event(_assistant({"type": "text", "text": "first: ok\nsecond: ok\n"}))
    assert line == "first: ok · second: ok"


# --- helpers ----------------------------------------------------------------


def test_summarize_tool_first_present_key_wins():
    # Bash has no file_path, so the loop falls through to command.
    block = {"name": "Bash", "input": {"command": "gh pr diff 1", "timeout": 5}}
    assert sf._summarize_tool(block) == "Bash gh pr diff 1"


def test_subagent_dispatch_names_the_subagent():
    block = {"name": "Agent", "input": {"prompt": "long task text", "subagent_type": "role-a"}}
    assert sf._summarize_tool(block) == "Agent role-a"


def test_path_under_the_clone_is_shown_relative_to_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    roots = sf.clone_roots()
    clone = str(tmp_path.resolve())
    read = {"name": "Read", "input": {"file_path": f"{clone}/src/module.py"}}
    bash = {"name": "Bash", "input": {"command": f"ls {clone}/src"}}
    assert sf._summarize_tool(read, roots) == "Read src/module.py"
    assert sf._summarize_tool(bash, roots) == "Bash ls src"


def test_a_sibling_path_that_shares_the_clones_prefix_stays_whole():
    roots = ("/work/clone",)
    bash = {"name": "Bash", "input": {"command": "ls /work/clone-other/x /work/clone/y"}}
    assert sf._summarize_tool(bash, roots) == "Bash ls /work/clone-other/x y"


def test_the_clone_named_by_its_shell_spelling_is_shown_relative_too(tmp_path, monkeypatch):
    # The shell keeps the symlinked spelling in PWD while the agent may print it.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.chdir(real)
    monkeypatch.setenv("PWD", str(link))
    read = {"name": "Read", "input": {"file_path": f"{link}/src/module.py"}}
    assert sf._summarize_tool(read, sf.clone_roots()) == "Read src/module.py"


def test_long_path_keeps_its_file_name():
    deep = "/outside/" + "nested/" * 20 + "target_file.py"
    summary = sf._summarize_tool({"name": "Read", "input": {"file_path": deep}})
    assert summary.endswith("target_file.py")
    assert len(summary) <= len("Read ") + 80


def test_summarize_tool_unknown_tool_shows_name_only():
    assert sf._summarize_tool({"name": "MysteryTool", "input": {"foo": "bar"}}) == "MysteryTool"


def test_truncate_flattens_and_caps():
    assert sf.truncate("a\n  b   c") == "a b c"
    capped = sf.truncate("x" * 200, limit=80)
    assert len(capped) == 80
    assert capped.endswith("…")


# --- run(): the reconstruction contract -------------------------------------


def _ndjson(*events: dict) -> io.StringIO:
    import json

    return io.StringIO("\n".join(json.dumps(e) for e in events) + "\n")


def test_run_writes_result_field_to_raw_out(tmp_path):
    raw = tmp_path / "raw.txt"
    fence = '```json\n{"summary": "ok", "comments": []}\n```'
    rc = sf.run(
        _ndjson(_assistant({"type": "text", "text": "done"}), {"type": "result", "result": fence}),
        raw,
    )
    assert rc == 0
    assert raw.read_text() == fence


def test_run_writes_cost_to_cost_out(tmp_path):
    # ADR 0023 dogfood follow-up: cost was only ever visible as one line per
    # lens in the live log, so a review's total had to be hand-summed by eye.
    raw = tmp_path / "raw.txt"
    cost = tmp_path / "raw.txt.cost"
    sf.run(
        _ndjson({"type": "result", "result": "ok", "total_cost_usd": 0.446}),
        raw,
        cost_out=cost,
    )
    assert cost.read_text() == "0.446"


def test_tokens_sidecar_counts_the_subagent(tmp_path):
    # A lens spends nearly all of its tokens inside a subagent, and the sibling
    # `usage` field counts only the top-level conversation. Reading it here
    # measured the dispatcher and ignored the reviewer: a probe spawning three
    # subagents summed the same as one. modelUsage agrees with total_cost_usd,
    # so the two disagreeing is what exposed the gap. Numbers below are a real
    # result event's, shortened.
    raw = tmp_path / "raw.txt"
    cost = tmp_path / "raw.txt.cost"
    sf.run(
        _ndjson(
            {
                "type": "result",
                "result": "ok",
                "total_cost_usd": 0.247,
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 162,
                    "cache_creation_input_tokens": 12650,
                    "cache_read_input_tokens": 47671,
                },
                "modelUsage": {
                    "claude-opus-4-8": {
                        "inputTokens": 6,
                        "outputTokens": 166,
                        "cacheReadInputTokens": 53179,
                        "cacheCreationInputTokens": 27053,
                        "maxOutputTokens": 64000,
                        "contextWindow": 1000000,
                    }
                },
            }
        ),
        raw,
        cost_out=cost,
    )
    # modelUsage's four spend fields, not usage's 60,487 and not a sum that
    # swept in maxOutputTokens or contextWindow alongside them.
    assert (tmp_path / "raw.txt.tokens").read_text() == "80404"


def test_tokens_sidecar_sums_every_model(tmp_path):
    # A call that falls back or delegates to a second model reports one entry
    # per model; the operator's limit is drawn on by all of them.
    raw = tmp_path / "raw.txt"
    cost = tmp_path / "raw.txt.cost"
    sf.run(
        _ndjson(
            {
                "type": "result",
                "result": "ok",
                "total_cost_usd": 0.1,
                "modelUsage": {
                    "claude-opus-4-8": {"inputTokens": 10, "outputTokens": 1},
                    "claude-haiku-4-5": {"inputTokens": 100, "outputTokens": 2},
                },
            }
        ),
        raw,
        cost_out=cost,
    )
    assert (tmp_path / "raw.txt.tokens").read_text() == "113"


def test_run_without_cost_out_writes_nothing(tmp_path):
    raw = tmp_path / "raw.txt"
    sf.run(_ndjson({"type": "result", "result": "ok", "total_cost_usd": 0.446}), raw)
    assert not (tmp_path / "raw.txt.cost").exists()


def test_run_timeout_leaves_cost_out_unwritten(tmp_path):
    # No result event (killed on timeout): no cost file, matching --raw-out's
    # own empty-on-timeout contract.
    raw = tmp_path / "raw.txt"
    cost = tmp_path / "raw.txt.cost"
    sf.run(
        _ndjson({"type": "assistant", "message": {"content": []}}),
        raw,
        cost_out=cost,
    )
    assert not cost.exists()


def test_run_without_prefix_marks_lines_as_the_daemons(tmp_path, capsys):
    raw = tmp_path / "raw.txt"
    sf.run(
        _ndjson(_assistant({"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}})),
        raw,
    )
    err = capsys.readouterr().err
    assert err == "[pr-review-agent] → Read a.py\n"


def test_run_starts_every_line_with_the_prefix(tmp_path, capsys):
    # Several PRs share one terminal and log file, so a line that lost its
    # prefix (the second block of a multi-block event) would be unattributable.
    raw = tmp_path / "raw.txt"
    sf.run(
        _ndjson(
            _assistant(
                {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
                {"type": "text", "text": "read it"},
            ),
            {"type": "result", "result": "ok", "num_turns": 1, "total_cost_usd": 0.1},
        ),
        raw,
        prefix="P> ",
    )
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 3
    assert all(line.startswith("P> ") for line in lines)


def _ticking_clock(*readings: float):
    it = iter(readings)
    return lambda: next(it)


def _segment(result: str, turns: int, duration_ms: int, cost: float) -> dict:
    return {
        "type": "result",
        "result": result,
        "num_turns": turns,
        "duration_ms": duration_ms,
        "total_cost_usd": cost,
    }


def test_a_stream_of_several_results_closes_with_one_line_of_the_calls_totals(tmp_path, capsys):
    # Each result is one segment of the session: its own turns and active time,
    # and the session's cost so far, so only the sum, the last, and the clock
    # describe the whole call.
    raw = tmp_path / "raw.txt"
    cost = tmp_path / "raw.txt.cost"
    sf.run(
        _ndjson(
            _segment("first", 3, 2775, 0.1048),
            _assistant({"type": "text", "text": "waiting"}),
            _segment("second", 1, 1317, 0.1048),
            _segment("final", 1, 1405, 0.1059),
        ),
        raw,
        prefix="",
        cost_out=cost,
        clock=_ticking_clock(100.0, 345.4),
    )
    closing = [line for line in capsys.readouterr().err.splitlines() if line.startswith("done")]
    assert closing == ["done · 5 turns · 245s · $0.106"]
    assert raw.read_text() == "final"
    assert cost.read_text() == "0.1059"


def test_a_stream_with_no_result_prints_no_closing_line(tmp_path, capsys):
    raw = tmp_path / "raw.txt"
    sf.run(_ndjson(_assistant({"type": "text", "text": "working"})), raw, prefix="")
    assert capsys.readouterr().err == "working\n"


def test_run_truncated_stream_leaves_raw_out_empty(tmp_path):
    # No result event (agent killed mid-run): raw-out exists but is empty, so the
    # call site's empty-stdout check fires instead of parsing partial output.
    raw = tmp_path / "raw.txt"
    raw.write_text("stale contents from a prior run")
    rc = sf.run(
        _ndjson(_assistant({"type": "tool_use", "name": "Read", "input": {"file_path": "x"}})), raw
    )
    assert rc == 0
    assert raw.read_text() == ""


def test_run_skips_malformed_lines(tmp_path):
    raw = tmp_path / "raw.txt"
    stream = io.StringIO('not json\n\n{"type":"result","result":"clean"}\n')
    assert sf.run(stream, raw) == 0
    assert raw.read_text() == "clean"


def test_run_ignores_non_string_result(tmp_path):
    raw = tmp_path / "raw.txt"
    rc = sf.run(_ndjson({"type": "result", "result": None}), raw)
    assert rc == 0
    assert raw.read_text() == ""


# --- the pipe-under-pipefail timeout invariant (shell level) ----------------


def _piped(snippet: str) -> subprocess.CompletedProcess:
    lib = REPO_ROOT / "daemon" / "lib.sh"
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail; source {lib}; {snippet}"],
        capture_output=True,
        text=True,
    )


def test_timeout_propagates_through_formatter_pipe(tmp_path):
    # A killed agent must surface as 142 through `claude … | stream_format`, not be
    # masked by the formatter's own exit. pipefail carries the perl-alarm status
    # because the formatter drains EOF and exits 0.
    raw = tmp_path / "raw.txt"
    fmt = REPO_ROOT / "daemon" / "stream_format.py"
    result = _piped(f"run_with_timeout 1 sleep 10 | python3 {fmt} --raw-out {raw}")
    assert result.returncode == TIMEOUT_EXIT, result.stderr
    assert raw.read_text() == ""
