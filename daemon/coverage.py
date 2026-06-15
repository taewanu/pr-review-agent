"""Render review-coverage percentages for the per-PR status comment."""


def coverage_percent(reviewed: int, total: int) -> float:
    """Percent of a PR's changed files the daemon actually reviewed.

    `reviewed` is the count that passed the path filters; `total` is every
    changed file in the diff."""
    return 0.0 if total == 0 else reviewed / total * 100


def format_coverage(reviewed: int, total: int) -> str:
    """One-line coverage label for the status comment, e.g. `8/10 files (80%)`."""
    pct = coverage_percent(reviewed, total)
    return f"{reviewed}/{total} files ({pct:.0f}%)"
