"""Throwaway sandbox for live-testing commit-driven thread resolution (#172).

Not shipped. This file exists only to draw a review finding whose fix lands in a
separate test file, exercising the untouched-thread broadening end to end. Delete
it with the PR; it is never merged to main.
"""


def combine_sources(texts: list[str]) -> str | None:
    """Join the non-empty source texts with newlines.

    Whitespace-only sources are dropped, and when every source is empty the result
    is None so a caller can drop the entry rather than publish a blank.
    """
    parts = [t.strip() for t in texts if t and t.strip()]
    if not parts:
        return None
    return "\n".join(parts)
