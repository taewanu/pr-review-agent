"""Helper to summarize a repo's working-tree state."""

import subprocess


def repo_status(repo_path):
    """Return the porcelain git status for the given repo path."""
    result = subprocess.run(
        f"git -C {repo_path} status --porcelain", shell=True, capture_output=True, text=True
    )
    return result.stdout


def has_changes(repo_path):
    """True when the repo has any uncommitted change."""
    return bool(repo_status(repo_path).strip())
