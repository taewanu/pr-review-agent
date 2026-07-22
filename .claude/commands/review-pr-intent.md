---
description: Run the intent lens on a single PR and emit a JSON review payload (ADR 0035).
argument-hint: <pr-url> --diff <path> --intent <path>
---

Dispatch the `review-agent-intent` subagent on the PR. Pass through the arguments verbatim: the first positional is the PR URL, `--diff <path>` points to a file containing `gh pr diff` output for the same PR, and `--intent <path>` points to a file holding what the change says it does (its title, description, and any linked issue).

Emit the subagent's stdout unchanged. Do not summarize, reformat, or add a wrapping fence, `daemon/merge_findings.py` reads the last ` ```json ` block from the raw output, same as `extract_json.py` does for `/review-pr`.
