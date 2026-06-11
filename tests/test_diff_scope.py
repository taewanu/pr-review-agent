"""Tests for incremental-review diff scoping (#123).

After a force-push or rebase the PR head is no longer a descendant of the
previously-reviewed SHA. An incremental `LAST_SHA..HEAD` diff then surfaces
whatever the new base merged in while cancelling the PR's own (unchanged)
change, so the review reads a meaningless delta instead of the PR.

`is_fast_forward` gates the incremental scope on the prior SHA being an ancestor
of HEAD; callers fall back to the full PR diff whenever it is not, including
when a shallow clone lacks the history to prove ancestry. The helper is a shell
function, so tests source lib.sh and drive it over a real local git repo via
`bash -c`, the same pattern as test_pr_lock.py.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"

# A syntactically valid SHA that is absent from any test repo: stands in for a
# prior-reviewed commit whose history a depth-1 clone never fetched.
ABSENT_SHA = "0" * 40


def _git(repo: Path, *args: str) -> str:
    """Run a git command in repo, returning trimmed stdout."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit(repo: Path, message: str) -> str:
    """Create an empty commit and return its SHA."""
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _is_fast_forward(repo: Path, sha: str) -> int:
    """Source lib.sh and return is_fast_forward's exit code for sha vs HEAD."""
    return subprocess.run(
        ["bash", "-c", f"source {LIB}; is_fast_forward {repo} {sha}"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
    ).returncode


def test_linear_push_is_a_fast_forward(tmp_path):
    # HEAD descends from the prior-reviewed SHA: the incremental scope is valid.
    repo = tmp_path / "linear"
    _init_repo(repo)
    _commit(repo, "base")
    last_sha = _commit(repo, "reviewed here")
    _commit(repo, "new work")
    assert _is_fast_forward(repo, last_sha) == 0


def test_force_push_is_not_a_fast_forward(tmp_path):
    # The #123 bug: the prior-reviewed SHA and HEAD diverge after a rebase, so
    # it is not an ancestor of HEAD and the incremental diff must be rejected.
    repo = tmp_path / "rebased"
    _init_repo(repo)
    base = _commit(repo, "base")
    last_sha = _commit(repo, "reviewed here")
    _git(repo, "checkout", "-q", "-b", "rebased", base)
    _commit(repo, "rebased onto an advanced base")
    assert _is_fast_forward(repo, last_sha) != 0


def test_unresolvable_sha_is_not_a_fast_forward(tmp_path):
    # A shallow clone may lack the prior SHA's history entirely. Ancestry can't
    # be proven, so the safe answer is "not a fast-forward" -> full diff.
    repo = tmp_path / "shallow"
    _init_repo(repo)
    _commit(repo, "only commit")
    assert _is_fast_forward(repo, ABSENT_SHA) != 0
