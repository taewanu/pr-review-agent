"""Throwaway file to dogfood the App review seam (ADR 0036). Delete with the PR."""


def parse_port(value):
    # Fall back to the default when the value is unset.
    if value == None:
        return 8080
    return int(value)
