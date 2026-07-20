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
get a line, reasoning/hook/rate-limit noise is dropped.
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
    assert line == "→ Read: a/b.py"


def test_result_renders_closing_summary():
    line = sf.render_event(
        {"type": "result", "num_turns": 3, "duration_ms": 8200, "total_cost_usd": 0.1914}
    )
    assert line == "done · 3 turns · 8s · $0.191"


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
    assert line == "→ Grep: TODO\nfound it"


# --- helpers ----------------------------------------------------------------


def test_summarize_tool_first_present_key_wins():
    # Bash has no file_path, so the loop falls through to command.
    block = {"name": "Bash", "input": {"command": "gh pr diff 1", "timeout": 5}}
    assert sf._summarize_tool(block) == "Bash: gh pr diff 1"


def test_summarize_tool_unknown_tool_shows_name_only():
    assert sf._summarize_tool({"name": "MysteryTool", "input": {"foo": "bar"}}) == "MysteryTool"


def test_truncate_flattens_and_caps():
    assert sf._truncate("a\n  b   c") == "a b c"
    capped = sf._truncate("x" * 200, limit=80)
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


def test_run_without_label_emits_unprefixed_lines(tmp_path, capsys):
    # Default (ADR 0023): every existing caller (editor, reply-pr.sh) omits
    # --label, so its progress lines must read exactly as before this change.
    raw = tmp_path / "raw.txt"
    sf.run(
        _ndjson(_assistant({"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}})),
        raw,
    )
    err = capsys.readouterr().err
    assert err == "[pr-review-agent]   → Read: a.py\n"


def test_run_with_label_tags_each_line(tmp_path, capsys):
    # Concurrent lenses (ADR 0023) share one terminal; a label distinguishes
    # which lens a given line came from.
    raw = tmp_path / "raw.txt"
    sf.run(
        _ndjson(_assistant({"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}})),
        raw,
        label="correctness",
    )
    err = capsys.readouterr().err
    assert err == "[pr-review-agent]   [correctness] → Read: a.py\n"


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
