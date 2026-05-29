---
description: Reply to operator inline-comment replies on prior pr-review-agent findings.
argument-hint: <pr-url> --threads <path>
---

Dispatch the `review-agent-reply` subagent on the PR. Pass through the arguments verbatim: the first positional is the PR URL, and `--threads <path>` points to a JSON file containing the unaddressed reply threads to verify.

Emit the subagent's stdout unchanged. Do not summarize, reformat, or add a wrapping fence — the daemon's `reply-pr.sh` reads the last ` ```json ` block from the raw output.

Do not load `.pr-review.yaml` or attempt multi-agent merging here — this command dispatches the one hardcoded reply agent and forwards its output.
