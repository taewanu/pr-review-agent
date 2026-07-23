"""Helper to summarize a repo's working-tree state."""

import subprocess


def repo_status(repo_path):
    """Return the porcelain git status for the given repo path."""
    result = subprocess.run(
        f"git -C {repo_path} status --porcelain", shell=True, capture_output=True, text=True
    )
    return result.stdout
