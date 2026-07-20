#!/usr/bin/env python3
"""Render a `claude -p --output-format stream-json` ndjson stream for the operator.

Sits in the pipe between the agent and the parser (#176): reads stream-json events
on stdin, prints a one-line-per-event progress view to stderr so the foreground
terminal is no longer blind during a `claude -p` call (ADR 0011), and reconstructs
the agent's final text into --raw-out.

The reconstruction is faithful: the stream's terminal `result` event carries a
`result` field equal to the exact text that text-mode `claude -p` prints to stdout
(the final assistant message, including its trailing ```json fence). Writing that to
--raw-out leaves extract_json.py / resolve_threads.py / create_reply.py reading the
same bytes they read before, so no parser changes (#176).

Exit-code discipline: the call sites pipe `run_with_timeout claude … | stream_format`
under `set -o pipefail`, so the pipeline surfaces claude's own exit status (142 on
the SIGALRM timeout backstop) only if this formatter exits 0. It therefore exits 0
even on a truncated stream (claude killed mid-run, no result event); --raw-out is
left empty in that case, and the call site's existing `[[ -s ]]` check handles it.
"""

import argparse
import json
import sys
from pathlib import Path


def _truncate(text: str, limit: int = 80) -> str:
    """Flatten to one line and cap length, so a long command can't wrap the view."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summarize_tool(block: dict) -> str:
    """`→ Read foo.py` / `→ Bash: gh pr diff …`: tool name plus its most telling arg.

    The informative key differs per tool, so take the first one present rather than
    branching on the tool name (which would need a case per tool we ever see)."""
    name = block.get("name", "tool")
    inp = block.get("input", {})
    for key in ("file_path", "command", "pattern", "path", "url", "prompt"):
        if key in inp:
            return f"{name}: {_truncate(str(inp[key]))}"
    return name


def render_event(event: dict) -> str | None:
    """Map one stream-json event to a progress line for stderr, or None to skip it.

    The caller adds the prefix and flushes; a returned string may hold several lines
    joined by "\\n" when one event carries multiple content blocks.
    """
    etype = event.get("type")
    if etype == "result":
        turns = event.get("num_turns", "?")
        secs = event.get("duration_ms", 0) / 1000
        cost = event.get("total_cost_usd", 0)
        return f"done · {turns} turns · {secs:.0f}s · ${cost:.3f}"
    if etype != "assistant":
        # system/hook_*, user (tool_result), rate_limit_event: noise, skip.
        return None
    lines: list[str] = []
    for block in event.get("message", {}).get("content", []):
        btype = block.get("type")
        if btype == "tool_use":
            lines.append(f"→ {_summarize_tool(block)}")
        elif btype == "text":
            # Narrate the agent's prose live (it also reappears in the final result).
            text = block.get("text", "").strip()
            if text:
                lines.append(_truncate(text))
        # btype == "thinking": skip (reasoning noise)
    return "\n".join(lines) if lines else None


PREFIX = "[pr-review-agent]   "  # indent under the log_step markers (lib.sh)


def _emit(line: str, label: str | None = None) -> None:
    # label (ADR 0023 revision): concurrent lenses stream to the same terminal
    # at once now, so their live lines interleave; a label tags each line with
    # its source (e.g. "[correctness]") so a reader can still follow one lens.
    # None for every other caller (editor, reply-pr.sh) leaves output unchanged.
    tag = f"[{label}] " if label else ""
    print(f"{PREFIX}{tag}{line}", file=sys.stderr, flush=True)


def run(stream, raw_out: Path, label: str | None = None, cost_out: Path | None = None) -> int:
    # Truncate --raw-out up front so it always exists, matching the old
    # `>"$RAW_FILE"` redirect: an empty file on a no-result (timeout) stream, the
    # reconstructed agent text on a complete one.
    raw_out.write_text("")
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # stream-json should emit only ndjson; ignore stray lines defensively
            # rather than corrupting the progress view or the raw reconstruction.
            continue
        if event.get("type") == "result":
            result_text = event.get("result")
            if isinstance(result_text, str):
                raw_out.write_text(result_text)
            # cost_out (ADR 0023 dogfood follow-up): the per-call cost was only
            # ever visible as one line per lens in the live log; the operator
            # had to hand-sum it to see what a review actually cost. Written
            # only on a real result event, so a timeout (no result) leaves no
            # cost file, matching --raw-out's own empty-on-timeout contract.
            if cost_out is not None:
                cost = event.get("total_cost_usd")
                if isinstance(cost, (int, float)):
                    cost_out.write_text(str(cost))
                # Tokens sidecar (#209): the rate-limit an operator actually hits
                # is tokens, not dollars, so record the call's total token count
                # next to the cost, for the caller to sum the same way. Written
                # parallel to .cost, so a timeout leaves neither.
                #
                # Read from modelUsage, not the sibling `usage`: `usage` counts
                # only the top-level conversation, and every lens spends nearly
                # all of its tokens inside a subagent, so summing `usage` here
                # measured the dispatcher and ignored the reviewer. modelUsage
                # agrees with total_cost_usd, which is how the discrepancy
                # surfaced. Fields are named explicitly because modelUsage also
                # carries maxOutputTokens, a ceiling rather than a spend.
                tokens = 0
                for per_model in (event.get("modelUsage") or {}).values():
                    if not isinstance(per_model, dict):
                        continue
                    for field in (
                        "inputTokens",
                        "outputTokens",
                        "cacheReadInputTokens",
                        "cacheCreationInputTokens",
                    ):
                        value = per_model.get(field)
                        if isinstance(value, (int, float)):
                            tokens += value
                cost_out.with_suffix(".tokens").write_text(str(int(tokens)))
        rendered = render_event(event)
        if rendered is not None:
            _emit(rendered, label=label)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw-out",
        type=Path,
        required=True,
        help="path to write the reconstructed agent stdout (the parser's input)",
    )
    parser.add_argument(
        "--label",
        help="tag each progress line with this (e.g. a lens name) when concurrent"
        " callers share one terminal",
    )
    parser.add_argument(
        "--cost-out",
        type=Path,
        help="path to write this call's total_cost_usd, for the caller to sum across lenses",
    )
    args = parser.parse_args()
    return run(sys.stdin, args.raw_out, label=args.label, cost_out=args.cost_out)


if __name__ == "__main__":
    sys.exit(main())
