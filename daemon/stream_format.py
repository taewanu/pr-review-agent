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
import os
import re
import sys
import time
from pathlib import Path


def truncate(text: str, limit: int = 80) -> str:
    """Flatten to one line and cap length, so a long command can't wrap the view."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _truncate_left(text: str, limit: int = 80) -> str:
    """Cap a path from the left, so the file name at its end stays visible."""
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def clone_roots() -> tuple[str, ...]:
    """Every spelling of the agent's working directory (the per-PR clone), longest first.

    The agent reports resolved paths (/private/var/… on macOS) while the shell's
    PWD keeps the symlinked one (/var/…), so both are stripped."""
    cwd = os.getcwd()
    roots = {cwd, os.path.realpath(cwd)}
    pwd = os.environ.get("PWD")
    if pwd and os.path.realpath(pwd) == os.path.realpath(cwd):
        roots.add(pwd)
    return tuple(sorted(roots, key=len, reverse=True))


def relative(text: str, roots: tuple[str, ...]) -> str:
    """`text` with each clone root that starts a path made relative: `<root>/x` reads
    `x` and a bare `<root>` reads `.`, while `<root>-other/x` is another path and stays."""
    for root in roots:
        pattern = r"(?<![\w.~/-])" + re.escape(root) + r"(?:(/)|(?=[\s'\"`:;,)\]]|$))"
        text = re.sub(pattern, lambda m: "" if m.group(1) else ".", text)
    return text


def describe_tool(name: str, arg: str, roots: tuple[str, ...], *, path: bool = False) -> str:
    """`Read daemon/lib.sh`: a tool name and its argument on one line, a path capped
    from the left so its file name stays visible."""
    arg = " ".join(relative(arg, roots).split())
    return f"{name} {(_truncate_left if path else truncate)(arg)}"


def prose(text: str, roots: tuple[str, ...]) -> str | None:
    """The agent's prose on one line, its own line breaks kept as separators."""
    text = " · ".join(part.strip() for part in text.splitlines() if part.strip())
    return truncate(relative(text, roots)) if text else None


def _summarize_tool(block: dict, roots: tuple[str, ...] = ()) -> str:
    """`Read daemon/lib.sh` / `Bash gh pr diff …`: tool name plus its most telling arg.

    The informative key differs per tool, so take the first one present rather than
    branching on the tool name (which would need a case per tool we ever see)."""
    name = block.get("name", "tool")
    inp = block.get("input", {})
    for key in ("file_path", "command", "pattern", "path", "url", "subagent_type", "prompt"):
        if key in inp:
            return describe_tool(name, str(inp[key]), roots, path=key in ("file_path", "path"))
    return name


def render_event(event: dict, roots: tuple[str, ...] = ()) -> str | None:
    """Map one assistant event to progress lines for stderr, or None to skip it.

    A returned string may hold several lines joined by "\\n" when one event carries
    multiple content blocks. A `result` event renders nothing here: one call can
    emit several, so run() prints one closing line after the stream ends, and
    none when the stream held no result (a timeout or a crash), where the
    caller's own exit line speaks for the call.
    """
    if event.get("type") != "assistant":
        # result, system/hook_*, user (tool_result), rate_limit_event: skip.
        return None
    lines: list[str] = []
    for block in event.get("message", {}).get("content", []):
        btype = block.get("type")
        if btype == "tool_use":
            lines.append(f"→ {_summarize_tool(block, roots)}")
        elif btype == "text":
            # Narrate the agent's prose live (it also reappears in the final result).
            text = prose(block.get("text", ""), roots)
            if text:
                lines.append(text)
        # btype == "thinking": skip (reasoning noise)
    return "\n".join(lines) if lines else None


def closing_line(
    turns: int | None, seconds: float, cost: float | None, tokens: int | None = None
) -> str:
    """The one line that closes a call, naming only what the CLI reported."""
    parts = ["done"]
    if turns is not None:
        parts.append(f"{turns} turn" + ("" if turns == 1 else "s"))
    parts.append(f"{seconds:.0f}s")
    if tokens is not None:
        parts.append(f"{tokens:,} tokens")
    if cost is not None:
        parts.append(f"${cost:.3f}")
    return " · ".join(parts)


def emit(text: str, prefix: str) -> None:
    for line in text.split("\n"):
        print(f"{prefix}{line}", file=sys.stderr, flush=True)


def run(
    stream,
    raw_out: Path,
    prefix: str = "[pr-review-agent] ",
    cost_out: Path | None = None,
    clock=time.monotonic,
) -> int:
    # Truncate --raw-out up front so it always exists, matching the old
    # `>"$RAW_FILE"` redirect: an empty file on a no-result (timeout) stream, the
    # reconstructed agent text on a complete one.
    raw_out.write_text("")
    start = clock()
    roots = clone_roots()
    saw_result = False
    turns: int | None = None
    last_cost: float | None = None
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
            # A session that waits on subagents emits a result per segment, with
            # that segment's num_turns, a duration_ms that leaves out the wait,
            # and the session's total_cost_usd so far, so turns add up, cost
            # takes the last, and duration is the wall clock.
            saw_result = True
            if isinstance(event.get("num_turns"), int):
                turns = (turns or 0) + event["num_turns"]
            if isinstance(event.get("total_cost_usd"), (int, float)):
                last_cost = event["total_cost_usd"]
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
        rendered = render_event(event, roots)
        if rendered is not None:
            emit(rendered, prefix)
    if saw_result:
        emit(closing_line(turns, clock() - start, last_cost), prefix)
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
        "--prefix",
        default="[pr-review-agent] ",
        help="text to start each progress line with (run_agent passes the PR and phase)",
    )
    parser.add_argument(
        "--cost-out",
        type=Path,
        help="path to write this call's total_cost_usd, for the caller to sum across lenses",
    )
    args = parser.parse_args()
    return run(sys.stdin, args.raw_out, prefix=args.prefix, cost_out=args.cost_out)


if __name__ == "__main__":
    sys.exit(main())
