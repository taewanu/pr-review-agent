---
description: Judge whether a prior pr-review-agent Finding's defect is fixed at the PR's current HEAD.
argument-hint: <pr-url> --finding <path>
---

Dispatch the `review-agent-fix-check` subagent on the PR. Pass the arguments through verbatim: the first positional is the PR URL, and `--finding <path>` points to a JSON file describing the one prior Finding to judge.

Emit the subagent's stdout unchanged. Do not summarize, reformat, or add a wrapping fence: the daemon's `review-pr.sh` reads the last ` ```json ` block from the raw output.

This command judges one Finding per invocation, so the daemon can isolate and parallelize per-thread judgments (ADR 0017). Do not load `.pr-review.yaml` or merge multiple agents here.
