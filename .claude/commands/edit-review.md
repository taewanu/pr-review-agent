---
description: Run the editorial pass over a draft review and emit the edited decisions.
argument-hint: <pr-url> --diff <path> --payload <path>
---

Dispatch the `review-agent-editor` subagent on the PR. Pass through the arguments verbatim: the first positional is the PR URL, `--diff <path>` points to the diff the review agent saw, and `--payload <path>` points to the draft review payload (the JSON the review agent emitted) to edit.

Emit the subagent's stdout unchanged. Do not summarize, reformat, or add a wrapping fence: the daemon's `apply_edits.py` reads the last ` ```json ` block from the raw output.

Dispatch the one hardcoded editor agent and forward its output. Do not load `.pr-review.yaml` or merge multiple agents here.
