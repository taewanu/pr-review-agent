"""Tests for incremental-review diff scoping (#123, #149).

A re-review scopes the diff to `LAST_SHA..HEAD` so the agent only re-reads the
new commits. That scope is valid only when HEAD is a fast-forward of the
prior-reviewed SHA; after a force-push or rebase the tips diverge and the
incremental diff surfaces whatever the new base merged in while cancelling the
PR's own (unchanged) change, so callers must fall back to the full PR diff.

The per-PR clone is shallow (--depth=1), so it lacks the history to prove
ancestry locally and every clean fast-forward read as a force-push (#149). The
fast-forward decision therefore moves to GitHub's compare API, whose `status`
the server computes from full history. `_status_is_fast_forward` is the pure
decision over that status string; the shell `is_fast_forward` adapter only
fetches the status over the network and is covered by live dogfood, not here.
Driving the predicate needs neither a git repo nor the network: source lib.sh
and call it via `bash -c`, the same pattern as test_pr_lock.py.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"


def _status_is_fast_forward(status: str) -> int:
    """Source lib.sh and return _status_is_fast_forward's exit code for status."""
    return subprocess.run(
        ["bash", "-c", f"source {LIB}; _status_is_fast_forward {shlex.quote(status)}"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
    ).returncode


def test_ahead_status_is_a_fast_forward():
    # HEAD carries new commits on top of the prior SHA: incremental scope valid.
    assert _status_is_fast_forward("ahead") == 0


def test_identical_status_is_a_fast_forward():
    # Re-review at the same commit: trivially a fast-forward (empty increment).
    assert _status_is_fast_forward("identical") == 0


def test_diverged_status_is_not_a_fast_forward():
    # Force-push/rebase: the tips diverged, so reject the incremental scope.
    assert _status_is_fast_forward("diverged") != 0


def test_behind_status_is_not_a_fast_forward():
    # HEAD is behind the prior SHA: not a forward move, so not a fast-forward.
    assert _status_is_fast_forward("behind") != 0


def test_unknown_status_is_not_a_fast_forward():
    # A failed or empty compare call yields no usable status. Bias to the safe
    # full-diff fallback rather than scoping on an unverified ancestry.
    assert _status_is_fast_forward("") != 0
    assert _status_is_fast_forward("garbage") != 0
