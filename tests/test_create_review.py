"""Snapshot tests for daemon/create-review.sh's --dry-run payload.

Fixture is `tests/fixtures/post_review_snapshot/` and holds:
- anchored.json: 2 findings (single-line `important+bug`, range `nit+refactor`)
- unanchored.json: 1 finding (`pre_existing+polish`) routed to ## Findings outside the diff
- summary.txt: review summary
- expected_payload.json: payload when no findings were dropped (default path)
- expected_payload_dropped_2.json: payload when 2 forbidden-combo findings were dropped

Snapshots store live-derived values as placeholders: the banner/footer
identity as `__PROJECT_NAME__` / `__PROJECT_URL__`, and the pyproject
version as `__VERSION__`. The test normalizes the live identity and
version into those placeholders before comparing, so the snapshot passes
on the canonical clone, any fork, and across version bumps alike. Identity
itself is covered by `test_footer_reflects_git_remote_identity`.

Regenerate snapshots (derived values are normalized automatically — no
manual find/replace step):

    python tests/test_post_review.py
    python tests/test_post_review.py --dropped-combo 2

The `__main__` block at the bottom runs the daemon, applies `_strip_derived`
to the body, and writes the result to the matching `expected_payload*.json`.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "post_review_snapshot"
DAEMON = REPO_ROOT / "daemon"


FIXTURE_HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
FIXTURE_HEAD_REPO_URL = "https://github.com/example/example"


def _run_post_review(
    *extra_args: str,
    head_sha: str | None = FIXTURE_HEAD_SHA,
    head_repo_url: str | None = FIXTURE_HEAD_REPO_URL,
) -> dict:
    """Returns the --dry-run review payload (the object POSTed to the reviews
    API: body + comments, plus commit_id/event when set)."""
    args = [
        "bash",
        str(DAEMON / "create-review.sh"),
        "--owner",
        "example",
        "--repo",
        "example",
        "--number",
        "999",
        "--summary-file",
        str(FIXTURE / "summary.txt"),
        "--anchored",
        str(FIXTURE / "anchored.json"),
        "--unanchored",
        str(FIXTURE / "unanchored.json"),
    ]
    if head_sha is not None:
        args += ["--head-sha", head_sha]
    if head_repo_url is not None:
        args += ["--head-repo-url", head_repo_url]
    args += ["--dry-run", *extra_args]
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _git_remote_identity() -> tuple[str, str]:
    """Owner and repo derived from the local git origin — same parse the
    daemon does, used by the derive test to stay correct on any checkout."""
    url = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?$", url)
    assert match, f"unexpected git remote URL format: {url}"
    return match.group(1), match.group(2)


def _pyproject_version() -> str:
    """Version the daemon stamps into the preview banner, read from the same
    pyproject.toml create-review.sh greps. Normalized away in the snapshot so a
    version bump doesn't drift it."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return data["project"]["version"]


def _strip_derived(body: str) -> str:
    """Substitute live-derived values (git-remote identity, pyproject version)
    with the fixture's placeholders so the snapshot stays stable across forks
    and version bumps alike. Anchored on the exact banner/footer templates
    emitted by create-review.sh."""
    owner, repo = _git_remote_identity()
    version = _pyproject_version()
    return (
        body.replace(
            f"[{repo}](https://github.com/{owner}/{repo})",
            "[__PROJECT_NAME__](__PROJECT_URL__)",
        )
        .replace(
            f"[Report a problem](https://github.com/{owner}/{repo}/issues)",
            "[Report a problem](__PROJECT_URL__/issues)",
        )
        .replace(
            f"{repo} v{version} (preview release)",
            "__PROJECT_NAME__ v__VERSION__ (preview release)",
        )
    )


def test_dry_run_payload_matches_snapshot():
    actual = _run_post_review()
    actual["body"] = _strip_derived(actual["body"])
    expected = json.loads((FIXTURE / "expected_payload.json").read_text())
    assert actual == expected, (
        "create-review.sh --dry-run payload drifted from snapshot. "
        "Regenerate per the docstring if the change was intentional."
    )


def test_dry_run_payload_with_dropped_combo_matches_snapshot():
    # Locks in the ADR 0005 per-finding-failure rendering: an italic note sits
    # between the summary and `## Findings outside the diff` so the operator sees the
    # redaction in the body itself, not just in stderr.
    actual = _run_post_review("--dropped-combo", "2")
    actual["body"] = _strip_derived(actual["body"])
    expected = json.loads((FIXTURE / "expected_payload_dropped_2.json").read_text())
    assert actual == expected, (
        "create-review.sh --dry-run --dropped-combo 2 payload drifted from snapshot. "
        "Regenerate per the docstring if the change was intentional."
    )


def test_sentinel_present_and_matches_adr_0006_format():
    # ADR 0006 fixes the sentinel format so the dedup migration parser in
    # `daemon/poll.sh` can grep it byte-exactly. Locks the regex separately
    # from the full-payload snapshot so a format regression points at this
    # test, not at a scrolling JSON diff.
    body = _run_post_review()["body"]
    match = re.search(r"<!-- pr-review-agent:sha:([0-9a-f]{40}) -->", body)
    assert match, "ADR 0006 sentinel missing from review body"
    assert match.group(1) == FIXTURE_HEAD_SHA
    # Sentinel sits below the operator footer so it parses out cleanly without
    # mid-body false positives.
    footer_idx = body.index("Submit, edit, or cancel as needed.")
    assert match.start() > footer_idx


def test_sentinel_omitted_when_head_sha_unset():
    # Dry-run / debug invocations may omit --head-sha. The script should emit
    # no sentinel rather than a malformed `pr-review-agent:sha:` with an empty
    # value the parser would never accept.
    body = _run_post_review(head_sha=None)["body"]
    assert "pr-review-agent:sha:" not in body


def test_additional_finding_location_links_to_head_blob():
    # A relocated finding is unanchored to the diff, so its location code span is
    # the only pointer back to the source. It links to the file at the head
    # commit (fork-correct via --head-repo-url), matching the reply blob link.
    # The visible label stays the `path:line` code span; only the wrap is added.
    body = _run_post_review()["body"]
    expected = (
        f"- [`src/api/auth.py:5`]"
        f"({FIXTURE_HEAD_REPO_URL}/blob/{FIXTURE_HEAD_SHA}/src/api/auth.py#L5)"
    )
    assert expected in body


def test_additional_finding_location_bare_when_head_unset():
    # Dry-run / debug invocations may omit --head-sha or --head-repo-url. The
    # location degrades to the bare code span rather than emitting a link with a
    # missing sha or repo, mirroring the sentinel's omit-when-unset rule.
    for kwargs in ({"head_sha": None}, {"head_repo_url": None}):
        body = _run_post_review(**kwargs)["body"]
        assert "- `src/api/auth.py:5`" in body
        assert "/blob/" not in body


def test_own_pr_submits_comment_review():
    # ADR 0008: own PRs auto-submit a COMMENT review in the create POST itself,
    # via an `event` field. Others' PRs omit it and stay pending.
    review = _run_post_review("--own-pr")
    assert review.get("event") == "COMMENT"
    # Others' path (default) carries no event key — the review stays pending.
    assert "event" not in _run_post_review()


def test_own_pr_footer_says_edit_not_submit_or_delete():
    # The review is already submitted, so the action line is post-hoc, not the
    # pre-submit submit/cancel of a pending review. It says "edit" rather than
    # "delete" because GitHub rejects deleting a submitted review (REST and
    # GraphQL both 422); an unwanted one can only be edited or have its comments
    # hidden.
    body = _run_post_review("--own-pr")["body"]
    assert "Edit as needed." in body
    assert "delete" not in body.lower()
    assert "Submit, edit, or cancel as needed." not in body
    assert "🤖 Auto-submitted by" in body


def test_footer_reflects_git_remote_identity():
    # Zero-config path: the daemon parses the local git origin to fill the
    # footer/banner. Body must surface that derived identity. Test stays
    # correct on canonical and fork checkouts alike by querying git directly.
    owner, repo = _git_remote_identity()
    body = _run_post_review()["body"]
    assert f"[{repo}](https://github.com/{owner}/{repo})" in body


if __name__ == "__main__":
    import sys

    extra = sys.argv[1:]
    payload = _run_post_review(*extra)
    payload["body"] = _strip_derived(payload["body"])
    suffix = "_dropped_2" if "--dropped-combo" in extra else ""
    out = FIXTURE / f"expected_payload{suffix}.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"regenerated {out.name}")
