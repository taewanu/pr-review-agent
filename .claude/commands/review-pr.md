---
description: Review a single PR end-to-end and emit a JSON review payload.
argument-hint: <pr-url> --diff <path>
---

Dispatch the `review-agent-default` subagent on the PR. Pass through the arguments verbatim: the first positional is the PR URL, and `--diff <path>` points to a file containing `gh pr diff` output for the same PR.

Emit the subagent's stdout unchanged. Do not summarize, reformat, or add a wrapping fence — the daemon's `extract-json.py` reads the last ` ```json ` block from the raw output.

Slice 1 dispatches a single hardcoded agent. Phase 3+ will load `.pr-review.yaml`, resolve the `agents:` list, dispatch in parallel, and merge payloads.
