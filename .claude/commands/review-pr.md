---
description: Review a single PR end-to-end and emit a JSON review payload.
argument-hint: <pr-url> --diff <path> --context <path>
---

Dispatch the `review-agent-default` subagent on the PR. Pass through the arguments verbatim: the first positional is the PR URL, `--diff <path>` points to a file containing `gh pr diff` output for the same PR, and `--context <path>` points to the shared context pack (ADR 0029).

Emit the subagent's stdout unchanged. Do not summarize, reformat, or add a wrapping fence — the daemon's `extract_json.py` reads the last ` ```json ` block from the raw output.

Do not load `.pr-review.yaml` or attempt multi-agent merging here — this command dispatches the one hardcoded agent and forwards its output.
