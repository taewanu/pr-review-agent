# ADR 0036: GitHub App identity for the daemon

Date: 2026-07-22
Status: Accepted. Supersedes [ADR 0003](./0003-identity-model.md) (identity model), [ADR 0004](./0004-own-pr-review-default.md) (own-PR review default), and [ADR 0008](./0008-own-pr-auto-submit.md) (own-PR auto-submit).

## Context

[ADR 0003](./0003-identity-model.md) chose Option A: the daemon authenticates as the operator's own GitHub user account, so every review, finding, and reply posts under a human name. It named Option C, a registered GitHub App, "a reserved future option for organization-scale deployments." This ADR takes Option C, and not for scale.

Option A costs more than the cosmetics of a self-review badge. Because the daemon and the operator share one login, GitHub cannot tell them apart, and everywhere the daemon needs to recognize its own work it has to fall back on something other than authorship:

- `daemon/detect-replies.jq:33` gates on a Provenance tag in the comment body because `user.login` matches the operator on both the daemon's reply ack and the operator's reply. That ambiguity is the root of the #153 self-reply bug, where an ack was redispatched as an operator reply.
- The pending-review path exists for the same reason. A review drafted under the operator's name on a colleague's PR needs a human gate before it publishes ([ADR 0008](./0008-own-pr-auto-submit.md)), which forks the daemon on own-versus-others and drags along the band-aid cluster ADR 0008 documents: the web submit modal blanks the body (#50), so the body is mirrored to a comment (#49), and a transient pickup-ack covers the gap (#48).
- [ADR 0004](./0004-own-pr-review-default.md)'s `REVIEW_OWN_PRS` flag and `GITHUB_USER` exist to answer "is this PR mine," a question that only arises when the reviewer and the author can be the same person.

A distinct bot login removes the ambiguity these work around, rather than working around it better. The delivery model that carries it is settled outside this ADR and not reopened here: a self-hosted polling App with per-user credentials, never a hosted multi-tenant service.

Three findings from the live probes shape the decision. Detail is on [#235](https://github.com/taewanu/pr-review-agent/issues/235) (capabilities), [#236](https://github.com/taewanu/pr-review-agent/issues/236) (registration), and [#237](https://github.com/taewanu/pr-review-agent/issues/237) (the access seam).

1. **Authorship is not a barrier.** The App edits comments a user wrote, reacts to them, and resolves threads a user opened. Nothing in the existing pipeline needs the operator's token to reach the operator's own artifacts.
2. **Thread resolution requires `contents: write`.** The App cannot resolve any thread, including one it opened itself, without it. The blocker was permission, not ownership.
3. **The JWT leg cannot go through `gh`.** `gh api` sends `Authorization: token <value>`, which the App-level endpoints reject; they require `Bearer`. Minting is `openssl dgst -sha256 -sign` plus `curl`. The installation token that comes back works with `gh` normally.

## Decision

The daemon authenticates as a self-hosted GitHub App and posts as `<app>[bot]`.

1. **A full swap, not a dial.** The App replaces the operator token outright. Splitting identity per PR or per repo was rejected: installation is per-repo, so splitting by PR author means deliberately downgrading to a personal account on repos where the App is already installed, and it preserves every own-versus-others branch this decision exists to delete.

2. **The App is personal-owned, and named for tone rather than function.** The canonical instance is `youshallnotmerge`, posting as `youshallnotmerge[bot]`; forks pick their own name. An organization-owned App would read as team infrastructure while one person's machine and subscription carry it, and two people running the same key would post as one bot. Because the name carries no function, the footer alone has to carry attribution and the project link.

3. **The permission set is `contents: write`, `issues: write`, `pull_requests: write`, `metadata: read`.** No webhook, no event subscriptions: the daemon polls. `issues: write` is there because the Status comment is an issue comment. `contents: write` buys thread resolution and nothing else, which is a code-write permission accepted for conversation closing alone. It is safe only because decision 5 keeps the token out of the environment the review agents run in.

4. **The installation ID is derived, never configured.** `GET /repos/{owner}/{repo}/installation` returns it per repository, and its 404 is exactly the missing-installation case. A watched repo without the App installed is skipped with a warning at boot, not a daemon-wide failure: a missing installation is a permanent per-repo state, so it belongs in the boot surface rather than accumulating as per-PR failures.

5. **The installation token is injected per call, never exported.** A `gh` wrapper in `daemon/lib.sh` attaches it to one command at a time, so the review agents that `claude -p` spawns never inherit it. They do not need it, since the diff and intent files are handed to them, and every agent definition grants unrestricted `Bash`, so an inherited token would be write access to every installed repository. Stripping the variable before each spawn was the alternative and fails silently when someone forgets; the wrapper fails loudly instead.

6. **Reviews submit immediately on every PR.** The pending-draft path existed because reviews carried the operator's own name. A pending review is invisible to anyone but its author, so it cannot serve a repo with other people on it, and under a bot identity there is no name to protect. `REVIEW_OWN_PRS`, `GITHUB_USER`, and `daemon/submit-review.sh` retire with it.

7. **Attribution derives from the App owner**, not from the git remote and not from a config value. The footer names who runs the bot, and its wording is rewritten for a reader who is not the operator. The AI-drafted marker glyph [ADR 0003](./0003-identity-model.md) introduced is no longer load-bearing: it marked drafted content sitting under a human account, and the `[bot]` suffix now carries that signal, so the glyph is free to become brand rather than disclosure. Which artifacts keep a glyph, and which one, is settled alongside the footer rewrite.

8. **Author identity replaces body-text self-identification.** The Provenance tag became the own-comment gate in `daemon/detect-replies.jq:33` only because a shared login could not separate the daemon's reply ack from the operator's reply (#153). A distinct bot login answers that directly, and without the tag's false positive: an operator who quotes the agent's comment, tag included, currently has their reply silently dropped. The tag's remaining role is presentation. The SHA sentinel ([ADR 0006](./0006-sentinel-based-dedup.md)) and the resolution stamp ([ADR 0019](./0019-resolution-as-in-place-state.md)) carry facts rather than identity and are unaffected; ADR 0006's discovery filter keeps its shape, with only the login value changing from the operator's to the bot's.

9. **The bot answers any non-bot reply on its own threads, with no mention gate.** A mention gate would miss the unmentioned fix claim ("Fixed in abc123"), which is the case where verifying the claim against HEAD is worth the most.

## Considered and rejected

- **A dedicated secondary user account** ([ADR 0003](./0003-identity-model.md) Option B). It buys a distinct login without the App registration, but requires a second GitHub account with its own email and PAT, and it cannot create check runs or carry per-repo installation. Rejected in ADR 0003 and not revived.
- **A legacy-login config key** so the daemon keeps recognizing pre-swap artifacts. It would heal open PRs across the swap, but it threads a second login through four call sites to solve what draining open PRs solves for free, and it makes a transitional state permanent by living in `.env`.
- **A replacement for `REVIEW_OWN_PRS`.** See the first consequence: no per-author filter addresses the failure mode that remains.

## Consequences

- **The `REVIEW_OWN_PRS` opt-out is deleted with no replacement.** It existed so two operators running daemons on one repo would not both review the same PR ([ADR 0004](./0004-own-pr-review-default.md): "Team-context operators set `review_own_prs: false`"). Under App identity the equivalent control is per-repo, matching how installation works: install one App per repository. Two installations produce two reviews on every PR, and no per-author filter would prevent that, since skipping your own PRs still leaves both daemons reviewing everyone else's.

- **Pre-swap agent state is orphaned.** Four sites test `user.login` to find the agent's own artifacts: sentinel discovery (`daemon/lib.sh:463`), Status comment discovery (`daemon/lib.sh:1105`), reply-parent matching (`daemon/detect-replies.jq:28`), and thread resolution (`daemon/resolve_threads.py`). After the swap each looks for the bot, while every pre-swap artifact carries the operator's login. A PR open at that moment gets a second Status comment with an empty findings index and SHA trail ([ADR 0020](./0020-findings-index-in-status-comment.md), [ADR 0021](./0021-reviewed-shas-trail-in-status-comment.md)), re-reviews from base, and can never dispatch a reply or resolve a thread on its old findings. The first two effects heal on the next cycle; the last two do not. **Deployment: merge or close open PRs before the swap.** Draining is free and removes the class.

- **`contents: write` is granted for conversation closing alone.** [ADR 0017](./0017-commit-driven-thread-resolution.md) and [ADR 0020](./0020-findings-index-in-status-comment.md) rest on thread resolution, so the permission is load-bearing rather than speculative, and `coderabbitai` requests the same entry plus five more. Anyone revisiting decision 5 should read this first: the seam is what makes the permission safe.

- **Onboarding gets harder before it gets easier.** `gh auth login` plus one config line becomes: register the App, set four permissions, disable the webhook, download the private key to `~/.pr-review-agent/app.pem` at mode 0600, and install on each watched repo. Collapsing that to a manifest flow is out of scope here.

- **The checks and statuses surface opens but is not taken.** Check runs are App-only, which is why #48 abandoned the check-run route for a comment, and they are the one mechanism that can hold a merge back. Their inline annotations do not substitute for review comments, since they carry no reply or resolve, so the value is the gate rather than the annotation. The permissions come when the feature does.

- **Whether the installation token clones a private repository is untested.** Every installed repo is public today, so the probe had nothing to run against.

> **Amended 2026-07-27 (#308, [ADR 0039](./0039-review-state-on-the-checks-row.md)).** `checks: write` joins decision 3's permission set, making it five. The last consequence above is what changes: the checks surface is taken, and the permission comes with the feature as that line said it would. It buys the check run that carries the review's state and nothing else, and decision 5's seam keeps it out of the review agents' environment exactly as it keeps `contents: write`. Existing installations must approve the added permission; a token minted before approval carries the old set, and the run then degrades to no checks row rather than failing the review.
