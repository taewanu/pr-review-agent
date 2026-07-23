---
description: Run the data-flow lens on a single PR and emit a JSON review payload (ADR 0023).
argument-hint: <pr-url> --diff <path>
---

Dispatch the `review-agent-data-flow` subagent on the PR. Pass through the arguments verbatim: the first positional is the PR URL, and `--diff <path>` points to a file containing `gh pr diff` output for the same PR.

Emit the subagent's stdout unchanged. Do not summarize, reformat, or add a wrapping fence, `daemon/merge_findings.py` reads the last ` ```json ` block from the raw output, same as `extract_json.py` does for `/review-pr`.
