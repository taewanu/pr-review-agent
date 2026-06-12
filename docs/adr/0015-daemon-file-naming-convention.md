# ADR 0015: Daemon file naming — language sets case, GitHub verbs set role

Date: 2026-06-12
Status: Accepted

## Context

`daemon/` had drifted into mixed naming that no longer signalled file role. Two problems compounded:

- **Case was inconsistent across Python files.** `voice.py` and `post_reply.py` were snake_case; `extract-json.py`, `anchor-findings.py`, and `load-config.py` were kebab. The kebab form is not a valid Python identifier (a hyphen parses as subtraction), so those files could not be `import`ed; every test loaded them through `importlib.util.spec_from_file_location("extract_json", path)`, assigning the snake module name the kebab file could not carry. `voice.py` had to be snake because it is `import`ed at runtime by both `extract_json.py` and `create_reply.py`. The on-disk name was fighting the module name the codebase already used.

- **The two review-posting scripts were indistinguishable by name.** `post-review.sh` and `submit-review.sh` both read as "send the review to GitHub," yet they are different API calls: `POST /pulls/{n}/reviews` (GitHub's "Create a review") versus `POST /reviews/{id}/events` (GitHub's "Submit a review"). `post-review.sh`'s own header even called its create step "submit," colliding with the genuine submit script.

The tracking issue (#131) framed `post_reply.py` as the snake outlier to bring in line with the kebab shell entrypoints. That reading is backwards: by language convention `post_reply.py` was already correct, and the real outliers were the three kebab Python scripts.

## Considered options

- **Directory decides — kebab everything**: treat all of `daemon/` as a flat set of CLI entrypoints and unify on kebab, renaming `post_reply.py` to `post-reply.py`. Rejected: `voice.py` must stay snake (it is imported), so the rule needs a permanent exception precisely where it matters, and the kebab files keep diverging from the snake module names every test already assigns. The only gain, a uniform `ls daemon/`, is illusory the moment `voice.py` breaks it.
- **Language decides — snake `.py`, kebab `.sh` (chosen)**: Python files follow PEP 8 snake_case; shell files stay kebab. Import-safe by construction, so a script later promoted to an imported module needs no rename (as `voice.py` already demonstrates). Costs three renames now.

## Decision

Two rules govern every file under `daemon/`.

**Rule 1 — language sets case.** Python files are `snake_case`, shell files are `kebab-case`. The file extension already tells the reader the language; the case follows each language's own convention.

**Rule 2 — the verb signals the role.** Posters that write to GitHub take the platform's own API verb: `create-*` makes the object, `submit-*` finalizes a pending one. Transform scripts keep their action verb (`extract`, `anchor`, `load`); orchestrators keep `<verb>-pr`.

Applied as a rename map:

| Role | Before | After |
|---|---|---|
| Transform | `extract-json.py` | `extract_json.py` |
| Transform | `anchor-findings.py` | `anchor_findings.py` |
| Transform | `load-config.py` | `load_config.py` |
| Poster | `post-review.sh` | `create-review.sh` |
| Poster | `post_reply.py` | `create_reply.py` |

`submit-review.sh`, `voice.py`, `lib.sh`, `run.sh`, `poll.sh`, `review-pr.sh`, `reply-pr.sh`, and `notify-slack.sh` were already conformant and are unchanged.

This yields a symmetric, learn-once layout for the two posting paths:

```
review:  review-pr.sh  →  create-review.sh  →  submit-review.sh
reply:   reply-pr.sh   →  create_reply.py
```

The reply path has no `submit-*` stage because replies are never gated; they post directly (ADR 0008). The asymmetry is meaningful, not an omission.

## Consequences

- File names now agree with the module names the tests already use, and the `create-*` / `submit-*` split is read straight off GitHub's API vocabulary instead of guessed.
- Naming is import-safe and future-proof: any transform script later promoted to a shared module imports without a rename.
- Blast radius was mechanical: imports, `poll.sh` / `review-pr.sh` / `reply-pr.sh` call sites, the `create_reply` function and its test references, and doc/ADR mentions, all updated in the same change; the 317-test suite pins the round-trips.
- The deviation worth recording is the absence of a uniform `ls daemon/` case: snake `.py` beside kebab `.sh` is deliberate, so a future contributor does not "fix" it to one case and reintroduce the unimportable kebab Python.

Tracked in #131.
