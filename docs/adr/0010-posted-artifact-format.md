# ADR 0010: Posted-artifact format — Review footer vs Provenance tag

Date: 2026-06-08
Status: Accepted. The #132 Amended note's "Reply review body" bullet is superseded by ADR 0019 (#159): reply acks now post detached, so no Reply review wrapper or disposition summary exists to carry a Provenance tag. The other artifact formats stand.

## Context

The daemon posts several artifact types, and their `🤖` footers drifted into four strings in two styles:

- `🤖 Auto-submitted by [name](url). Edit as needed.` — own-PR Review body (`create-review.sh`)
- `🤖 Drafted by [name](url). Submit, edit, or cancel as needed.` — others'-PR Review body (`create-review.sh`)
- `🤖 _AI-drafted_` — Inline comment (`create-review.sh`)
- `🤖 _pr-review-agent_` — reply (`create_reply.py`, set by #82)

Two problems compound the inconsistency. First, `_AI-drafted_` is applied unconditionally (`create-review.sh` ignores `OWN_PR`), so a finding on an auto-submitted own-PR review labels itself a draft when it is already posted — the same flaw #82 fixed for replies. Second, the Review-body footer string was only ever an implementation detail: `create-review.sh` cites "ADR 0001 D3", but D3 decides *one pending review per tick*, not the footer text. The format was never pinned.

The shared voice rules (opener words, 두괄식, no em dash, no task-scoped refs) have the same shape of drift: defined in `review-agent-default.md`, partly re-copied into `review-agent-reply.md`, and enforced post-hoc only for reviews (`extract_json.py`), not for replies. The severity/type badge map is the counter-example — single-sourced in `create-review.sh` (`SEV_EMOJI`/`TYPE_EMOJI`, ADR 0002) and shared by both renders. That is the model the rest should follow.

## Decision

### 1. Two tiers, split by artifact level — not by draft-status

- **Review body** carries a **Review footer**: attribution plus the next action, varying on the pending/posted axis. Others' PR (genuinely pending) → `🤖 Drafted by [name](url). Submit, edit, or cancel as needed.`; own PR (auto-submitted, ADR 0008) → `🤖 Auto-submitted by [name](url). Edit as needed.`
- **Every other posted artifact** — Inline comment, reply, Status comment — carries the **Provenance tag** `🤖 _pr-review-agent_`. It answers only "who wrote this" and never encodes draft-status, so it is byte-identical on pending and posted artifacts.

The governing principle: **draft-status is a review-level fact, stated once in the Review footer; provenance is an item-level fact, stated per artifact as the Provenance tag.** This generalizes #82's reply fix into a rule and removes the `_AI-drafted_` misnomer (a posted item is not a draft).

### 2. Per-post-type anatomy

| Post type | Anatomy (top → bottom) |
|---|---|
| Review body (others', pending) | `[preview banner]` · summary · `[dropped-findings note]` · `[## Findings outside the diff]` · **Review footer** (Drafted) · Sha sentinel |
| Review body (own, auto-submitted) | same, **Review footer** (Auto-submitted) |
| Inline comment | `_type_ \| _severity_` badge · agent body (bold lead + optional bullets) · **Provenance tag** |
| Reply | agent body (italic lead, see §4 amendment) · `[blob-at-HEAD link]` · **Provenance tag** · Reply sentinel |
| Status comment | head line · scope · file list (`<details>`) · **Provenance tag** · Status marker |

> **Amended 2026-06-12 (#132).** A format audit across all posted artifacts reconciled three drifts this draft left or predated:
> - **Reply review body** (the disposition-summary wrapper, #38/#11) postdates this ADR and is not a Finding Review body: it carries no Findings, no Sha sentinel, and is always auto-submitted. It falls under §1's "every other posted artifact" and now carries the **Provenance tag** between the summary and the hidden `reply-review` marker, matching the acks it wraps. A Review footer is moot here: there is no draft-status to state (always submitted) and no submit/edit/cancel action on a rollup line.
> - **Review footer 🤖 placement** aligns to the Provenance tag: the robot sits *outside* the emphasis span (`🤖 _Drafted by …_`, not `_🤖 Drafted by …_`), so both attribution shapes share one "🤖 then italic" form. Visually near-identical (an emoji has no italic form); the win is one markup convention, not a rendered change.
> - **Findings-outside-the-diff item** adopts the Inline comment's badge-first order, joined to the location link by a colon: `_🐛 bug_ | _🔴 important_: [`path:line`](url)`. This mirrors the #106 verdict colon-into-link. The location led before (it was the only pointer for an unanchored finding); the badge now leads for triage parity with the Inline comment, the link still one line away.

### 3. Single source for the Provenance tag

This ADR is the documentary source of truth; each renderer hard-codes the tag with a comment referencing ADR 0010, and a test pins the three emit sites (`create-review.sh` Inline comment, `create_reply.py`, `lib.sh` Status comment) to one identical string. A runtime shared constant is rejected: the renderers straddle the bash/jq–Python boundary, and coupling them at runtime to share one short string costs more than a drift test. (The Review-footer strings live at a single site, `create-review.sh`, so they need no cross-file test.)

### 4. Voice rules: one prompt source, symmetric enforcement

- ~~`review-agent-default.md` is the SSOT for the shared voice rules; `review-agent-reply.md` references it and drops its re-copied prose.~~ **Superseded by the 2026-07-21 amendment below**, which moves the SSOT to `review-agent-editor.md` and records why the cross-file reference never resolved at runtime. Mode-specific leads (`confirmed`/`pushback`/`stands`/`withdrawn`) stay local to the reply agent.
- The post-hoc voice checks move to a shared `daemon/voice.py` imported by both `extract_json.py` and `create_reply.py`. Reply bodies are validated at the extraction gate with the Inline-comment rules (`strip_bold=True`, `FORBIDDEN_PREFIXES`, em dash, task-ref); a violation raises `style-violation` and **fails the whole reply batch before any POST**, symmetric with `extract_json.py`'s atomic-payload model and reusing the existing "no sentinel → retry next cycle" path. Only the opener/em-dash/task-ref rules are enforced; the bold-lead *shape* is not, matching `extract_json.py`.

> **Amended 2026-06-09 (#100).** Inline comment and reply bodies now additionally enforce the **structural 2–4 bullet count** (`check_bullets`): a body carrying bullets must have 0 or 2–4, never one or 5+. This is the structural half of the shape and is losslessly checkable. The *semantic* shape is still not enforced — the validator never forces a body to lead with bold or to use bullets, since "this reasoning is multi-point, so it should be bulleted" is a judgment a post-hoc check can only false-positive on. So the §4 line above narrows to: opener/em-dash/task-ref on every field, plus bullet *count* on bodies; the decision to bullet stays a prompt convention.

> **Amended 2026-06-09 (#106).** Reply verdict leads diverge from the Inline comment's bold lead: they are **italic** (`_…_`), carry **no trailing period**, and `confirmed` uses the colon form `_Confirmed:_` so the verdict reads into the blob link. This ends the shared bold-lead shape between the two artifacts (the Inline comment stays bold per ADR 0002); a lighter italic lead suits a short threaded ack. The opener rule still applies — `voice.split_lead` and the `strip_bold` peel were generalized to recognize `_…_` as well as `**…**` by CommonMark flanking (#104), so a forbidden opener inside an italic lead still trips. The italic-vs-bold choice itself is shape, not validated.

> **Amended 2026-06-13 (#144).** The shared voice rules in `review-agent-default.md` were strengthened along seven semantic facets, all prompt-side; the `voice.py` boundary §100 drew is unchanged. The facets sharpen each lens: clarity-theater (a smooth, jargon-free finding that still names no actionable defect fails the clear lens), the concise qualifier/WHY safeguard and its terse-but-cryptic twin (cut filler, never the words a reader needs), elegance as the lowest-priority lens (never trade accuracy or the point for a smoother sentence, and never force parallel structure on non-parallel ideas), the ornament-is-filler false-fail, and 두괄식 applied fractally (each finding and each bullet leads with its own point, and an opener that only announces a conclusion is not a lead). Each is a judgment a lexical or structural check can only false-positive on, so each strengthens the prompt SSOT and leaves `voice.py` (opener, em dash, task-ref, bullet count) untouched. The facet text lived in `review-agent-default.md`, not here; this note records only that strengthening it is a prompt change, not a validator one. (It moved with the rest of the voice rules in the 2026-07-21 amendment below.)

> **Amended 2026-07-21 (#226).** The voice SSOT moves from `review-agent-default.md` to `review-agent-editor.md`, because the prompt pointer §4 established never resolved at runtime. Every agent runs as its own process loading exactly one definition file: `review-pr.sh` builds a single-agent system prompt from `.claude/agents/review-agent-<label>.md` alone, and a subagent likewise loads only its own file. So each "same voice as `review-agent-default`" reference in the four lens agents, the editor, the reply agent, and the fix-check agent pointed at text those agents never received. Only the default lens held the rules, and its draft is not what posts: the editor rewrites every surviving body (ADR 0016) and is the last author before the gate. The rules now live where the prose is finalized, carried as verbose-to-tight example pairs rather than rule prose, since style transfers by imitation. Generator lenses keep only the mechanical shape (bold lead, 0 or 2-4 bullets, no em dash, no task-scoped refs) and name `voice.py` as its source. The reply and fix-check agents state their own artifact's shape inline: an italic one-line ack and a bold-lead finding are different artifacts sharing only the mechanical rules. The `voice.py` boundary §100 drew is unchanged.

> **Amended 2026-07-23 (#277).** The bold lead names the **problem, then the fix**, reversing the earlier "lead with the fix" rule. A finding whose lead is the fix makes a reader reverse-engineer the defect from the solution; leading with the defect and its cost puts the stakes first, which is what a reviewer triaging a PR scans for (what is wrong, how bad, how to fix). This sharpens what 두괄식 means for a finding: the point is the problem it names, not the action it recommends. The change is prompt-side only, carried in `review-agent-editor.md`'s example pairs and the generator lenses' shape line; the `voice.py` boundary §100 drew is unchanged, since problem-first vs fix-first is a semantic judgment a lexical or structural check can only false-positive on. The forbidden-opener rule composes with it: a defect lead still may not open on `This`/`The`/`It`, so it leads on the symbol or the defect noun (`` `repo_path` runs as a shell command ``), not an article.

## Out of scope (the boundary)

This ADR fixes format only. It explicitly does **not** decide:

- **What is posted** — content and behavior are unchanged. The one intended behavioral consequence is the new reply voice gate (a previously-posting em-dash reply now fails and retries); that is the enforcement-parity goal, not a content change.
- **#38** — wrapping per-cycle replies in one `COMMENTED` review. The per-item Provenance-tag contract holds regardless of later batching; if #38 introduces a wrapper body, that body's footer is #38's call, but it may not reopen the per-item tag.
- **#75** — thread auto-resolution.
- **Moving the `create-review.sh` jq render into Python** to share the tag constant at runtime (the rejected option in §3). A reasonable follow-on refactor, tracked separately.

## Consequences

- The four `🤖` strings collapse to two shapes: one Review footer (two action variants) and one Provenance tag. The `_AI-drafted_` string is gone.
- `daemon/voice.py` becomes the single home for voice validation; adding a rule updates one module and both text-post paths inherit it.
- A reply the agent writes with an em dash, a forbidden opener, or a task-scoped ref no longer posts; it fails the batch and the next polling cycle re-runs the reply agent. This is the same failure-then-retry behavior reviews already have.
- CONTEXT.md gains the **Review footer** and **Provenance tag** terms; the bare "marker" stays reserved for the hidden Sentinel/Status markers.
- No `reply-pr.sh` change is needed: it already feeds any `category=` line from `create_reply.py` to `log_failure`, so `style-violation` flows through. ADR 0005's failure table is broadened to name reply bodies.
