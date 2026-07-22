# ADR 0003: Identity model for review comments

Date: 2026-05-19
Status: Superseded by [ADR 0036](./0036-github-app-identity.md). Option C replaces Option A outright; the AI-drafted marker and the project-link footer are re-decided there, the marker's purpose reassigned from disclosure to brand now that the `[bot]` suffix carries the signal.

## Context

GitHub binds PR review comment authorship to the API token used in the POST request. The displayed comment author is determined by whose token signed the call: `git config user.name/email` is irrelevant here, as that only affects commit objects.

Three candidate identity models were considered:

- **A: Operator's own user account.** The daemon uses the operator's existing `gh auth` token. Comments appear under the operator's name.
- **B: Dedicated secondary user account.** A second GitHub user account is created (for example, `<name>-bot`) and its PAT is used. Comments appear under that secondary account.
- **C: GitHub App registration.** A registered GitHub App is installed in the target repos; comments appear as `<app-name>[bot]`. This is the model used by both Anthropic Code Review and CodeRabbit.

Trade-offs:

- Option A has zero setup cost but produces self-reviews on the operator's own PRs.
- Option B avoids self-review labeling but requires managing a second GitHub account with a separate email, separate PAT, and separate distribution overhead for forks.
- Option C produces the cleanest UI (clear bot identity) but breaks the "no GitHub App registration" property and is operationally heaviest.

## Decision

Use Option A for V1: the daemon authenticates as the operator's own GitHub user account via the existing `gh` CLI session. Each review comment includes a body marker indicating AI-drafted content, and the review summary includes a footer linking to the project. The exact marker glyph and footer text are implementation choices not pinned in this ADR, so they can be tuned without re-issuing the decision. No second account is created. No GitHub App is registered.

Option C remains a reserved future option for organization-scale deployments that explicitly want a bot identity. Option B is rejected outright: maintaining a secondary GitHub account is operationally awkward and the value over Option A is captured by the pending-review safety net described in [ADR 0004](./0004-own-pr-review-default.md).

## Consequences

- Setup cost is minimal: the operator's existing `gh auth` token is the only credential the daemon needs.
- Self-review labeling on the operator's own PRs is real but bounded by the pending review state: nothing is publicly visible until the operator submits, and the operator can edit, delete, or cancel the entire review before publication.
- The body marker makes AI-drafted content visually distinguishable in the GitHub UI even though the author label is the operator's name. The summary footer links back to the project.
- Forks reuse the operator's identity model unchanged. No per-fork bot-account creation.
- The human-submit step is unique to Option A: the operator clicks "Finish your review" in GitHub's web modal, which posts an empty `body` if the textarea is left blank and silently overwrites the drafted summary (`PUT`-recovery returns `422`). Bot-identity tools (Option C) never reach this step. Safe submit paths are documented in the README ("Submitting a review").
- A later switch to Option C is feasible: review agent output and daemon flow are unchanged; only the credential and the posting account change. The body marker and footer can be retained or removed at that point.
