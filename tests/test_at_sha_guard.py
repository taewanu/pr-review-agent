"""Tests for review-pr.sh's --at-sha argument guard.

The guard is one branch inside a script that runs top to bottom against a live
PR, so it cannot be sourced for a single decision. It is lifted into a bash
snippet here, the same approach test_intent_lens.py takes, and the tests pin the
outcome rather than the expression so an equivalent rewrite does not fail.

Why it exists: a short sha reached `git fetch` as an unknown ref and failed with
a raw "couldn't find remote ref", and `git log` is where an operator copies one.
"""

from __future__ import annotations

import subprocess

FULL_SHA = "8f1d0134327fed8c52b90b6f399aae6808b15aba"


def _accepts(value: str) -> bool:
    """Whether review-pr.sh's --at-sha guard admits `value`."""
    script = f'''
    if [[ ! "{value}" =~ ^[0-9a-f]{{40}}$ ]]; then
      echo reject
    else
      echo accept
    fi
    '''
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return out.stdout.strip() == "accept"


def test_a_full_sha_is_accepted():
    assert _accepts(FULL_SHA)


def test_an_abbreviated_sha_is_rejected():
    # The shape operators actually paste, and what `git log --oneline` prints.
    assert not _accepts("8f1d013")
    assert not _accepts("deadbeef")


def test_a_ref_name_is_rejected():
    # Not a sha at all: these would reach `git fetch` and fail obscurely too.
    assert not _accepts("main")
    assert not _accepts("HEAD~1")


def test_an_uppercase_or_overlong_sha_is_rejected():
    # git prints lowercase, and 41 hex characters is not a sha even though the
    # first 40 are. An unanchored pattern would admit both.
    assert not _accepts(FULL_SHA.upper())
    assert not _accepts(FULL_SHA + "a")


def test_the_guard_matches_the_script():
    """The lifted pattern must stay identical to the one review-pr.sh runs."""
    source = subprocess.run(
        ["grep", "-c", r"\^\[0-9a-f\]{40}\$", "daemon/review-pr.sh"],
        capture_output=True,
        text=True,
    )
    assert source.stdout.strip() == "1", "the guard moved or changed shape"
