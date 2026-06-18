"""Bucket a review finding into drop / review / surface by its signal mix.

The review summary needs one verdict per finding before it is posted. A finding
carries three signals: how many independent review angles agreed on it, how many
verification passes refuted it, and its severity. This maps that mix to a band so
the summary path does not re-derive the rule at each call site.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Signals:
    agreements: int
    refutations: int
    severity: str  # "high" | "medium" | "low"


def confidence_band(s: Signals) -> str:
    """Return the band for a finding: "drop", "review", or "surface".

    A finding refuted at least as often as it is agreed is dropped. Otherwise a
    high-severity finding, or one with a net agreement of two or more, is surfaced
    outright; anything weaker is held for a closer human review.
    """
    net = s.agreements - s.refutations
    if net <= 0:
        return "drop"
    if s.severity == "high":
        return "surface"
    if net >= 2:
        return "surface"
    return "review"
