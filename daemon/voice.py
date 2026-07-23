"""Shared post-hoc voice checks for daemon text posts (ADR 0010).

extract_json.py validates the Review body's summary and each Inline comment;
create_reply.py validates each reply body. Both enforce the same rules — no em
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


# Inline code spans, stripped before the literal-newline check. A body may
# legitimately show `` `\n` `` as code, which is not a corruption.
_CODE_SPAN_RE = re.compile(r"`[^`]*`")


def fidelity_violation(text: str) -> str | None:
    """Return a message if `text` carries a reserialization corruption, else None.

    When an Editor agent reserializes a body (#133, ADR 0016) it occasionally
    HTML-escapes `<`/`>`/`&` or writes the two literal characters backslash-n in
    place of a real newline. Both are invisible to the lexical voice rules but
    losslessly checkable, so the gate rejects them.

    Entities are checked on the full text: the corruption seen in the trial put
    `&lt;` inside a code span (`scrollTop &lt;= 0`) where the raw `<` belonged, so
    stripping spans would miss it. A body that legitimately teaches the entity is
    vanishingly rare in review prose; fail-closed and retry is fine. The literal
    backslash-n corruption replaces real newlines, so it shows up outside code
    spans; spans are stripped before that check to spare a body that shows a
    backslash-n sequence as code.
    """
    for entity in ("&lt;", "&gt;", "&amp;"):
        if entity in text:
            return f"HTML-escapes {entity} (write the character raw)"
    if "\\n" in _CODE_SPAN_RE.sub("", text):
        return "contains a literal backslash-n outside a code span (use a real newline)"
    return None


# A top-level bullet: a column-0 `- ` marker. Indented continuations and prose
# hyphens never start at column 0 with a trailing space, so they don't count.
BULLET_RE = re.compile(r"^- ", re.MULTILINE)


def bullet_count_violation(text: str) -> str | None:
    """Return a message if `text`'s top-level bullets break the 2–4 rule, else None.

    Inline comment and reply bodies carry 0 or 2–4 `- ` bullets: never exactly
    one (a lone bullet is a sentence with extra weight) and never 5+ (past four
    you are listing, not pointing), per review-agent-editor §Voice. This is
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
    check_fidelity: bool = False,
    label: str,
) -> list[str]:
    """Return human-readable voice violations for one text field.

    `label` names the field in each message (e.g. "summary", "comments[2].body",
    "replies[0].body"). Order is em dash, then opener, then task ref, then bullet
    count, then fidelity, so callers that concatenate fields keep a stable
    message order.

    `check_bullets` adds the structural 2–4 bullet rule (bullet_count_violation);
    set it for Inline comment and reply bodies, leave it off for the summary,
    which has its own looser 0-or-1+ bullet rule. `check_fidelity` adds the
    reserialization-corruption rule (fidelity_violation); set it on any field an
    Editor agent may re-emit (#133, ADR 0016).
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
    if check_fidelity and (msg := fidelity_violation(text)) is not None:
        violations.append(f"{label} {msg}")
    return violations


# Artifact types. Each names a posted surface whose voice rule set this module
# owns, so a call site validates by artifact and never re-assembles the flags
# itself. SUMMARY / INLINE_COMMENT / REPLY_BODY mirror ADR 0010 §4; RESOLUTION_STAMP
# is the later fix-rationale surface (resolve_threads.py), folded in here so no
# posting path assembles voice flags on its own.
SUMMARY = "summary"
INLINE_COMMENT = "inline-comment"
REPLY_BODY = "reply-body"
RESOLUTION_STAMP = "resolution-stamp"

# The per-artifact rule matrix: the opener set, whether a bold or italic lead is
# peeled before the opener scan, and whether the structural 2–4 bullet count
# applies. The em dash and task-ref checks run on every artifact, so they are not
# matrix flags. Reserialization fidelity is not here either: the post-Editor gate
# routes it through fidelity_violations so a corruption fails the review while a
# cosmetic miss only warns. INLINE_COMMENT and REPLY_BODY share a rule set (reply
# bodies validate with the Inline-comment rules, ADR 0010 §4 and its #100/#106
# amendments) but stay distinct keys, since they are distinct artifacts a future
# divergence would split.
_ARTIFACT_RULES: dict[str, dict[str, object]] = {
    SUMMARY: {"prefixes": FORBIDDEN_SUMMARY_PREFIXES, "strip_bold": False, "check_bullets": False},
    INLINE_COMMENT: {"prefixes": FORBIDDEN_PREFIXES, "strip_bold": True, "check_bullets": True},
    REPLY_BODY: {"prefixes": FORBIDDEN_PREFIXES, "strip_bold": True, "check_bullets": True},
    RESOLUTION_STAMP: {"prefixes": FORBIDDEN_PREFIXES, "strip_bold": False, "check_bullets": True},
}


def check_artifact(artifact: str, text: str, *, label: str | None = None) -> list[str]:
    """Voice violations for one field, under the rule set assigned to `artifact`
    (SUMMARY / INLINE_COMMENT / REPLY_BODY / RESOLUTION_STAMP).

    The per-artifact entry point: a call site names the artifact, never the flags,
    so adding an artifact or changing a rule is one edit to _ARTIFACT_RULES.
    `label` names the field in each message and defaults to the artifact name.
    """
    return check_text(text, label=label or artifact, **_ARTIFACT_RULES[artifact])


def check_payload(summary: str, bodies: list[str]) -> list[str]:
    """Return the cosmetic voice violations for a final review payload.

    The two-artifact convenience for the review path: the SUMMARY plus each
    INLINE_COMMENT body, each validated under its own artifact rule set. Both
    `extract_json.py` (author parse) and `apply_edits.py` (post-Editor gate) call
    this so the review rules live in one place.

    Reserialization corruption is not checked here; `fidelity_violations` owns that
    rule, and the post-Editor gate routes it there separately so a corruption
    fails the review while a cosmetic miss only warns.
    """
    violations = check_artifact(SUMMARY, summary, label="summary")
    for i, body in enumerate(bodies):
        violations += check_artifact(INLINE_COMMENT, body, label=f"comments[{i}].body")
    return violations


def fidelity_violations(summary: str, bodies: list[str]) -> list[str]:
    """Return only the reserialization-corruption violations for a final payload.

    The subset of check_payload that signals a genuinely broken payload (an
    Editor that HTML-escaped a character or wrote a literal backslash-n for a
    newline, #133) rather than a cosmetic voice miss (a forbidden opener, an em
    dash, a bullet count). The post-Editor gate fails the review on these but
    only warns on the cosmetic rest: a review that correctly found a real bug
    must not be discarded because its summary opens with "The" — style is a thing
    to polish, not a gate that drops findings. Corruption is different: it means
    the text the reader would see is malformed, so it stays fail-closed.
    """
    out = []
    for label, text in [
        ("summary", summary),
        *((f"comments[{i}].body", b) for i, b in enumerate(bodies)),
    ]:
        if (msg := fidelity_violation(text)) is not None:
            out.append(f"{label} {msg}")
    return out
