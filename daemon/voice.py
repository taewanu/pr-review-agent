"""Shared post-hoc voice checks for daemon text posts (ADR 0010).

extract-json.py validates the Review body's summary and each Inline comment;
post_reply.py validates each reply body. Both enforce the same rules — no em
dash, no forbidden sentence openers, no task-scoped refs — so the rules live
here once and both import them.

The opener / em-dash / task-ref rules are enforced on every field; Inline
comment and reply bodies additionally get the structural 2–4 bullet-count rule
(check_bullets). What stays unvalidated is the *semantic* shape: a body may ship
plain prose with no bold lead and no bullets and still pass — the validator
never forces a body to bullet, it only checks the count when bullets are
present. Opener voice is the load-bearing rule; reaching for the bold-lead-plus-
bullets shape (ADR 0002) is a prompt convention.
"""

from __future__ import annotations

import re

EM_DASH = "—"

# Openers the voice prompt forbids. Trailing space distinguishes "This "
# (demonstrative opener) from words like "Thinking".
FORBIDDEN_PREFIXES = (
    "This ",
    "The ",
    "It ",
    "Worth ",
    "Suggest ",
    "Please ",
    "Consider ",
    "Maybe ",
)
# Summaries stay plain prose, so a leading bold is itself forbidden there. Inline
# comment and reply bodies lead with a bold sentence, so they peel it instead
# (strip_bold=True) and use FORBIDDEN_PREFIXES.
FORBIDDEN_SUMMARY_PREFIXES = ("**",) + FORBIDDEN_PREFIXES

# Task-scoped identifiers rot the moment the slice ships and the PR description
# already carries the same context. Case-sensitive on Slice/Phase so lowercase
# code prose ("phase=5", "slice the array") doesn't trip. ADR numbers and
# external standards (RFC, ISO) are stable references and intentionally not
# matched here.
TASK_REF_PATTERNS = (
    re.compile(r"\bSlice \d+\b"),
    re.compile(r"\bPhase \d+\b"),
    re.compile(r"\bStory #\d+\b"),
    re.compile(r"\bPRD #?\d+\b"),
)


# A leading emphasis lead: `**bold**` or `_italic_`, anchored at start with a
# lazy body. The close must not be immediately followed by a word char, matching
# how GitHub's CommonMark renderer treats a right-flanking delimiter run: this
# skips the intra-word `_` in snake_case identifiers (`some_helper`) and hardens
# `**` against a stray non-emphasis run (e.g. a `**/` glob). Off-spec shapes like
# `***bold-italic***` or `*italic*` are intentionally not matched.
_LEAD_RE = re.compile(r"\A(\*\*.+?\*\*|_.+?_)(?!\w)", re.DOTALL)


def _peel_lead_delims(stripped: str) -> str:
    """Strip the leading `**` or `_` emphasis opener from `stripped`, or return
    it unchanged. Mirrors the openers split_lead recognizes; only the exact `**`
    or `_` shape is peeled (not `***` or `*italic*`)."""
    if stripped.startswith("**"):
        return stripped[2:].lstrip()
    if stripped.startswith("_"):
        return stripped[1:].lstrip()
    return stripped


def forbidden_prefix(
    text: str, prefixes: tuple[str, ...], *, strip_bold: bool = False
) -> str | None:
    """Return the forbidden opener `text` starts with, or None.

    With strip_bold, peel a leading `**` or `_` emphasis opener before the scan so
    a word-level opener hidden inside an emphasis lead still trips (`**This …**`
    and `_This …_` must fail like `This …`). Re-lstrip after the peel: the opener
    hid any inner whitespace from the first lstrip, so `**  This …**` would
    otherwise slip past. Only the exact `**` or `_` shape is peeled; off-spec
    leads like `***bold-italic***` or `*italic*` are not.
    """
    stripped = text.lstrip()
    if strip_bold:
        stripped = _peel_lead_delims(stripped)
    for prefix in prefixes:
        if stripped.startswith(prefix):
            return prefix
    return None


def split_lead(text: str) -> tuple[str, str]:
    """Split a leading emphasis sentence (`**bold**` or `_italic_`) from `text`.

    Returns (lead, rest): `lead` is the emphasis span with its delimiters, `rest`
    is the remaining prose lstripped. When `text` has no recognizable closed lead
    (no opener, or an opener with no closer), returns ("", text) so the caller
    keeps the body whole. The closing delimiter is matched the way GitHub's
    CommonMark renderer matches emphasis (right-flanking: not followed by a word
    char), so an intra-word `_` in a snake_case identifier inside an italic lead
    is not mistaken for the closer. Recognizes only the exact `**` / `_` shapes,
    like the peel in forbidden_prefix(strip_bold=True).
    """
    stripped = text.lstrip()
    match = _LEAD_RE.match(stripped)
    if match is None:
        return "", text
    lead = match.group(1)
    return lead, stripped[match.end() :].lstrip()


def find_task_ref(text: str) -> str | None:
    """Return the first task-scoped ref found in `text`, or None."""
    for pattern in TASK_REF_PATTERNS:
        if (match := pattern.search(text)) is not None:
            return match.group(0)
    return None


# A top-level bullet: a column-0 `- ` marker. Indented continuations and prose
# hyphens never start at column 0 with a trailing space, so they don't count.
BULLET_RE = re.compile(r"^- ", re.MULTILINE)


def bullet_count_violation(text: str) -> str | None:
    """Return a message if `text`'s top-level bullets break the 2–4 rule, else None.

    Inline comment and reply bodies carry 0 or 2–4 `- ` bullets: never exactly
    one (a lone bullet is a sentence with extra weight) and never 5+ (past four
    you are listing, not pointing), per review-agent-default §Body shape. This is
    the *structural* half of the shape — it checks the count of bullets that are
    present, not the semantic decision of whether to bullet (a multi-sentence
    single-point body legitimately uses 0). summary keeps its own 0-or-1+ rule
    and is exempt, so callers set check_bullets only for comment/reply bodies.
    """
    n = len(BULLET_RE.findall(text))
    if n == 1:
        return "has a single bullet (use one sentence, or 2–4 bullets)"
    if n > 4:
        return f"has {n} bullets (2–4 max)"
    return None


def check_text(
    text: str,
    *,
    prefixes: tuple[str, ...],
    strip_bold: bool = False,
    check_bullets: bool = False,
    label: str,
) -> list[str]:
    """Return human-readable voice violations for one text field.

    `label` names the field in each message (e.g. "summary", "comments[2].body",
    "replies[0].body"). Order is em dash, then opener, then task ref, then bullet
    count, so callers that concatenate fields keep a stable message order.

    `check_bullets` adds the structural 2–4 bullet rule (bullet_count_violation);
    set it for Inline comment and reply bodies, leave it off for the summary,
    which has its own looser 0-or-1+ bullet rule.
    """
    violations: list[str] = []
    if EM_DASH in text:
        violations.append(f"{label} contains em dash")
    if (prefix := forbidden_prefix(text, prefixes, strip_bold=strip_bold)) is not None:
        violations.append(f"{label} opens with forbidden prefix {prefix.rstrip()!r}")
    if (ref := find_task_ref(text)) is not None:
        violations.append(f"{label} contains task-scoped ref {ref!r}")
    if check_bullets and (msg := bullet_count_violation(text)) is not None:
        violations.append(f"{label} {msg}")
    return violations
