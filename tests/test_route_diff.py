"""Tests for daemon/route_diff.py's prose-vs-behaviour classification (#219).

The verdict only feeds a log line today, but it is the evidence a routing
decision would later rest on, so a wrong classification is a wrong dataset.
Every case here pins the safe-biased contract: unclassified is behaviour.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "daemon" / "route_diff.py"
_spec = importlib.util.spec_from_file_location("route_diff", MODULE_PATH)
assert _spec and _spec.loader
route_diff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(route_diff)


def _diff(*paths: str) -> str:
    return "".join(
        f"diff --git a/{p} b/{p}\nindex 1111111..2222222 100644\n"
        f"--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+new\n"
        for p in paths
    )


@pytest.mark.parametrize("path", ["README.md", "docs/adr/0022-x.md", "docs/guide.rst", "n.adoc"])
def test_prose_suffixes_are_prose(path):
    assert route_diff.path_is_prose(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "daemon/review-pr.sh",
        "daemon/extract_json.py",
        ".github/workflows/ci.yml",
        "templates/.env.example",
        "pyproject.toml",
        "src/main.zig",  # a suffix nobody classified still reaches the code role
        "LICENSE",  # no suffix at all
    ],
)
def test_everything_unclassified_is_behaviour(path):
    assert route_diff.path_is_prose(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "requirements.txt",  # a dependency manifest wearing a prose suffix
        "CMakeLists.txt",  # a build script wearing a prose suffix
        "uv.lock",  # a pin bump changes what runs
        "package-lock.json",
    ],
)
def test_manifests_are_behaviour_despite_looking_like_prose(path):
    assert route_diff.path_is_prose(path) is False


@pytest.mark.parametrize(
    "path",
    [
        ".claude/agents/review-agent-code.md",
        "vendor/checkout/.claude/agents/review-agent-intent.md",
        "CLAUDE.md",
        "docs/../CLAUDE.md",
        "AGENTS.md",
    ],
)
def test_instructions_are_behaviour_despite_the_md_suffix(path):
    # The review's own behaviour is written in markdown. Classifying it as prose
    # would drop the code role on a change to the code role itself.
    assert route_diff.path_is_prose(path) is False


def test_one_behaviour_path_among_prose_is_enough():
    assert route_diff.has_executable_change(_diff("README.md", "docs/x.md", "daemon/lib.sh"))


def test_all_prose_is_prose():
    assert route_diff.has_executable_change(_diff("README.md", "docs/x.md")) is False


def test_empty_diff_is_behaviour():
    assert route_diff.has_executable_change("") is True


def test_quoted_non_ascii_path_is_seen():
    # git C-quotes a non-ASCII path in both headers. Reading the `diff --git`
    # line instead would drop it, and a dropped .py path reads as prose that is
    # not there. This is the case that sent the first implementation wrong.
    diff = (
        'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
        '--- "a/caf\\303\\251.py"\n'
        '+++ "b/caf\\303\\251.py"\n'
        "@@ -1 +1 @@\n-a\n+b\n"
    ) + _diff("README.md")
    assert "café.py" in route_diff.diff_paths(diff)
    assert route_diff.has_executable_change(diff) is True


def test_space_bearing_path_is_seen():
    diff = (
        "diff --git a/docs/my notes b/x.py b/docs/my notes b/x.py\n"
        "--- a/docs/my notes b/x.py\t\n"
        "+++ b/docs/my notes b/x.py\t\n"
        "@@ -1 +1 @@\n-a\n+b\n"
    )
    assert route_diff.diff_paths(diff) == ["docs/my notes b/x.py"]


def test_deletion_yields_its_path_from_the_old_side():
    # An added or deleted file has /dev/null on one side; the other side carries
    # the path, so reading both markers is what keeps it.
    diff = (
        "diff --git a/daemon/gone.py b/daemon/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/daemon/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-old\n"
    )
    assert route_diff.diff_paths(diff) == ["daemon/gone.py"]
    assert route_diff.has_executable_change(diff) is True


def _cli(tmp_path, text: str) -> int:
    diff_file = tmp_path / "d.diff"
    diff_file.write_text(text)
    return subprocess.run(["python3", str(MODULE_PATH), str(diff_file)]).returncode


def test_cli_exits_zero_on_behaviour(tmp_path):
    assert _cli(tmp_path, _diff("daemon/lib.sh")) == 0


def test_cli_exits_one_on_prose_only(tmp_path):
    assert _cli(tmp_path, _diff("README.md")) == 1


def test_cli_treats_a_missing_file_as_behaviour(tmp_path):
    assert subprocess.run(["python3", str(MODULE_PATH), str(tmp_path / "nope")]).returncode == 0


def test_ledger_record_is_appended_and_parseable(tmp_path):
    # The ledger is what makes the observation survive a foreground run, so a
    # record that does not round-trip through jq answers nothing later.
    lib = REPO_ROOT / "daemon" / "lib.sh"
    subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; export PR_REVIEW_STATE_DIR={tmp_path}; source {lib}; "
            "record_route_observation 'https://example/pull/1' abc123 prose-only 0.42; "
            "record_route_observation 'https://example/pull/2' def456 behaviour 1.90",
        ],
        check=True,
    )
    lines = (tmp_path / "route-observations.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert [r["verdict"] for r in records] == ["prose-only", "behaviour"]
    assert [r["cost_usd"] for r in records] == [0.42, 1.90]
    assert records[0]["sha"] == "abc123"


def test_ledger_write_is_best_effort(tmp_path):
    # An absent state dir must not fail a review that otherwise succeeded.
    lib = REPO_ROOT / "daemon" / "lib.sh"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; export PR_REVIEW_STATE_DIR={tmp_path}/missing; source {lib}; "
            "record_route_observation 'https://example/pull/1' abc123 prose-only 0.42",
        ]
    )
    assert result.returncode == 0


def _rollup(tmp_path) -> str:
    lib = REPO_ROOT / "daemon" / "lib.sh"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; export PR_REVIEW_STATE_DIR={tmp_path}; source {lib}; "
            "log_route_ledger_rollup",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout + result.stderr


def test_rollup_counts_prose_reviews_and_their_spend(tmp_path):
    (tmp_path / "route-observations.jsonl").write_text(
        '{"ts":"t","pr":"p","sha":"a","verdict":"prose-only","cost_usd":0.42}\n'
        '{"ts":"t","pr":"p","sha":"b","verdict":"behaviour","cost_usd":1.90}\n'
        '{"ts":"t","pr":"p","sha":"c","verdict":"prose-only","cost_usd":0.58}\n'
    )
    out = _rollup(tmp_path)
    assert "2/3 reviews were prose-only" in out
    assert "$1.00" in out  # only the prose-only rows are summed


def test_rollup_is_silent_without_a_ledger(tmp_path):
    assert "routing ledger" not in _rollup(tmp_path)


def test_ledger_records_a_commit_once(tmp_path):
    # A failed tick logs its cost on the way out and the next tick retries the
    # same sha; counting that commit twice would bend the prose-only share.
    lib = REPO_ROOT / "daemon" / "lib.sh"
    subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; export PR_REVIEW_STATE_DIR={tmp_path}; source {lib}; "
            "record_route_observation 'https://example/pull/1' abc123 prose-only 0.10; "
            "record_route_observation 'https://example/pull/1' abc123 prose-only 0.42; "
            "record_route_observation 'https://example/pull/1' def456 behaviour 1.90",
        ],
        check=True,
    )
    records = [
        json.loads(line)
        for line in (tmp_path / "route-observations.jsonl").read_text().splitlines()
    ]
    assert [r["sha"] for r in records] == ["abc123", "def456"]
    assert records[0]["cost_usd"] == 0.10  # first write wins
