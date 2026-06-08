"""Shared post-hoc voice checks for daemon text posts (ADR 0010).

extract-json.py validates the Review body's summary and each Inline comment;
post_reply.py validates each reply body. Both enforce the same rules — no em
dash, no forbidden sentence openers, no task-scoped refs — so the rules live
here once and both import them.

Only the opener / em-dash / task-ref rules are enforced. The bold-lead *shape*
(ADR 0002) is a prompt convention, not validated: a body that ships plain prose
still passes. Opener voice is the load-bearing rule; the bold wrapper is shape.
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


def forbidden_prefix(
    text: str, prefixes: tuple[str, ...], *, strip_bold: bool = False
) -> str | None:
    """Return the forbidden opener `text` starts with, or None.

    With strip_bold, peel a leading `**…` before the scan so a word-level opener
    hidden inside a bold lead still trips (`**This …**` must fail like `This …`).
    Re-lstrip after the peel: `**` hid any inner whitespace from the first lstrip,
    so `**  This …**` would otherwise slip past. Only the exact `**` shape is
    peeled; off-spec leads like `***bold-italic***` or `*italic*` are not.
    """
    stripped = text.lstrip()
    if strip_bold and stripped.startswith("**"):
        stripped = stripped[2:].lstrip()
    for prefix in prefixes:
        if stripped.startswith(prefix):
            return prefix
    return None


def find_task_ref(text: str) -> str | None:
    """Return the first task-scoped ref found in `text`, or None."""
    for pattern in TASK_REF_PATTERNS:
        if (match := pattern.search(text)) is not None:
            return match.group(0)
    return None


def check_text(
    text: str, *, prefixes: tuple[str, ...], strip_bold: bool = False, label: str
) -> list[str]:
    """Return human-readable voice violations for one text field.

    `label` names the field in each message (e.g. "summary", "comments[2].body",
    "replies[0].body"). Order is em dash, then opener, then task ref, so callers
    that concatenate fields keep a stable message order.
    """
    violations: list[str] = []
    if EM_DASH in text:
        violations.append(f"{label} contains em dash")
    if (prefix := forbidden_prefix(text, prefixes, strip_bold=strip_bold)) is not None:
        violations.append(f"{label} opens with forbidden prefix {prefix.rstrip()!r}")
    if (ref := find_task_ref(text)) is not None:
        violations.append(f"{label} contains task-scoped ref {ref!r}")
    return violations
