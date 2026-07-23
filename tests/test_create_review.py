"""Snapshot tests for daemon/create-review.sh's --dry-run payload.

Fixture is `tests/fixtures/create_review_snapshot/` and holds:
- anchored.json: 2 findings (single-line `important+bug`, range `nit+refactor`)
- unanchored.json: 1 finding (`pre_existing+polish`) routed to ## Findings outside the diff
- summary.txt: review summary
- expected_payload.json: payload when no findings were dropped (default path)
- expected_payload_dropped_2.json: payload when 2 forbidden-combo findings were dropped

The footer is now deterministic from --app-slug (ADR 0036): the tests pass a fixed
slug, so the snapshot stores the literal body with no live-derived values to
normalize. The canonical `youshallnotmerge` slug draws a themed pool line keyed to
the fixture SHA; regenerating after a pool edit is expected.

Regenerate snapshots:

    python tests/test_create_review.py
    python tests/test_create_review.py --dropped-combo 2
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "create_review_snapshot"
DAEMON = REPO_ROOT / "daemon"


FIXTURE_HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
FIXTURE_HEAD_REPO_URL = "https://github.com/example/example"
# The canonical App slug drives the themed footer pool; snapshots pin its output.
CANONICAL_SLUG = "youshallnotmerge"


def _run_create_review(
    *extra_args: str,
    head_sha: str | None = FIXTURE_HEAD_SHA,
    head_repo_url: str | None = FIXTURE_HEAD_REPO_URL,
    app_slug: str | None = CANONICAL_SLUG,
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
    if app_slug is not None:
        args += ["--app-slug", app_slug]
    args += ["--dry-run", *extra_args]
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_dry_run_payload_matches_snapshot():
    actual = _run_create_review()
    expected = json.loads((FIXTURE / "expected_payload.json").read_text())
    assert actual == expected, (
        "create-review.sh --dry-run payload drifted from snapshot. "
        "Regenerate per the docstring if the change was intentional."
    )


def test_dry_run_payload_with_dropped_combo_matches_snapshot():
    # Locks in the ADR 0005 per-finding-failure rendering: an italic note sits
    # between the summary and `## Findings outside the diff` so the operator sees the
    # redaction in the body itself, not just in stderr.
    actual = _run_create_review("--dropped-combo", "2")
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
    body = _run_create_review()["body"]
    match = re.search(r"<!-- pr-review-agent:sha:([0-9a-f]{40}) -->", body)
    assert match, "ADR 0006 sentinel missing from review body"
    assert match.group(1) == FIXTURE_HEAD_SHA
    # Sentinel sits below the footer so it parses out cleanly without
    # mid-body false positives.
    footer_idx = body.index("github.com/apps/")
    assert match.start() > footer_idx


def test_sentinel_omitted_when_head_sha_unset():
    # Dry-run / debug invocations may omit --head-sha. The script should emit
    # no sentinel rather than a malformed `pr-review-agent:sha:` with an empty
    # value the parser would never accept.
    body = _run_create_review(head_sha=None)["body"]
    assert "pr-review-agent:sha:" not in body


def test_additional_finding_location_links_to_head_blob():
    # A relocated finding is unanchored to the diff, so its location code span is
    # the only pointer back to the source. It links to the file at the head
    # commit (fork-correct via --head-repo-url), matching the reply blob link.
    # The visible label stays the `path:line` code span; only the wrap is added.
    body = _run_create_review()["body"]
    # Badge-first, then the linked location after a colon (ADR 0010 §2, #132).
    expected = (
        f"- _✨ polish_ | _🟣 pre_existing_: [`src/api/auth.py:5`]"
        f"({FIXTURE_HEAD_REPO_URL}/blob/{FIXTURE_HEAD_SHA}/src/api/auth.py#L5)"
    )
    assert expected in body


def test_additional_finding_location_bare_when_head_unset():
    # Dry-run / debug invocations may omit --head-sha or --head-repo-url. The
    # location degrades to the bare code span rather than emitting a link with a
    # missing sha or repo, mirroring the sentinel's omit-when-unset rule.
    for kwargs in ({"head_sha": None}, {"head_repo_url": None}):
        body = _run_create_review(**kwargs)["body"]
        # Badge-first, bare code span (no link) after the colon (#132).
        assert "_✨ polish_ | _🟣 pre_existing_: `src/api/auth.py:5`" in body
        assert "/blob/" not in body


def test_review_submits_comment_immediately():
    # ADR 0036 decision 6: every review submits as a COMMENT in the create POST
    # itself, via the `event` field. There is no pending path and no own-vs-others
    # fork, so the event is always present.
    assert _run_create_review().get("event") == "COMMENT"


def test_canonical_slug_footer_is_a_themed_pool_line():
    # The canonical App draws a 🧙 pool line linking its profile (ADR 0036 4a).
    body = _run_create_review()["body"]
    assert f"](https://github.com/apps/{CANONICAL_SLUG})" in body
    assert "🧙 _" in body
    # No preview banner survives (deleted with the git-remote identity).
    assert "preview release" not in body


def test_other_slug_footer_is_the_plain_line():
    # A fork with any other slug gets one plain safety line, since the flavor is
    # tied to the canonical name.
    body = _run_create_review(app_slug="my-fork-app")["body"]
    assert "🤖 _Automated review by [my-fork-app](https://github.com/apps/my-fork-app)._" in body
    assert "🧙" not in body


if __name__ == "__main__":
    import sys

    extra = sys.argv[1:]
    payload = _run_create_review(*extra)
    suffix = "_dropped_2" if "--dropped-combo" in extra else ""
    out = FIXTURE / f"expected_payload{suffix}.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"regenerated {out.name}")
