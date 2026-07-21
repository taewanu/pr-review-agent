# Persona design

A persona is a review agent's identity profile — its Voice, Tone variation rules, and Nuance patterns (see [CONTEXT.md](../CONTEXT.md)). Convention extends the Mailchimp/Polaris voice & tone model with an additional nuance layer:

| Layer | Time scale | Codified in V1? |
| --- | --- | --- |
| **Voice** — fixed identity ("who the agent is") | Constant across the entire review | Yes, per agent (see each `.claude/agents/review-agent-*.md`) |
| **Tone** — situational shift in voice across review contexts (severity / type) | Varies per finding category | No — emerges from voice; may be pinned in a future release |
| **Nuance** — micro-variation within a given tone (word choice, sentence endings, rhythm) | Varies per finding | No — emerges naturally |

## V1 default voice (canonical example)

`review-agent-editor`, the last author before a review posts, uses Slack's "X but never Y" pattern:

- **Confident** — but never cocky
- **Witty** — but never silly
- **Conversational** — not formal, not corporate
- **Intelligent** — substantive, never hedging
- **Friendly** — warm, not cold
- **Helpful** — actionable, not preachy
- **Clear / concise / human** — accessible, brief, real

The "X but never Y" form is load-bearing — it sets both the trait and its guardrail in one phrase. Authoring future review agents (e.g., `review-agent-security`) should follow the same shape.

## Authoring a new review agent

Future work; only the default review agent ships today. Outline:

1. Define voice using the "X but never Y" pattern, 5–7 traits.
2. Decide whether tone variations across severity/type need explicit rules (most agents won't — voice naturally adapts).
3. Test the voice against PR samples — a noisy or condescending voice surfaces fast in finding bodies.

## Out of scope

- Tone Matrix–style explicit per-scenario voice tables (Meta's pattern). Future candidate.
- Voice A/B testing infrastructure.
- Per-user voice preferences (the operator picks an agent, not a per-PR voice override).
