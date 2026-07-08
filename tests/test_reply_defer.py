"""Tests for deferring a reply thread whose claimed fix commit is not in HEAD.

The reply race (sa#163): the operator commits a fix, replies naming that SHA,
and pushes, but the reply pass fetched HEAD before the push landed. Verifying
the pre-fix code pushes back on a correct fix. `reply_defers_on_unreachable_fix`
defers such a thread so a later cycle verifies it once the push arrives.

The predicate has two halves: extract the commit the reply claims (the precision
risk, since a false-positive hex would defer a legitimate thread forever), and
defer only when that commit is absent from the repo via `commit_exists`. A
commit that exists but diverged after a rebase still carries its fix into HEAD,
so it verifies rather than deferring forever. `commit_exists` hits the API and
is covered by live dogfood; here it is stubbed so the test drives extraction and
the defer decision without a git repo or the network. Stubbing it to
always-absent ties the defer outcome directly to "did we extract a real SHA," so
a spurious extraction surfaces as a wrong defer.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"


def _defers(reply_body: str, *, exists: bool) -> bool:
    """True when the thread defers. `exists` stubs commit_exists's verdict."""
    exists_rc = 0 if exists else 1
    script = (
        f"source {LIB}; "
        f"commit_exists() {{ return {exists_rc}; }}; "
        f"reply_defers_on_unreachable_fix {shlex.quote(reply_body)} example/example"
    )
    return (
        subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"]},
        ).returncode
        == 0
    )


def _claimed_sha(reply_body: str) -> str:
    """The SHA passed to commit_exists (lowercased), or "" when none is claimed."""
    script = (
        f"source {LIB}; "
        f'commit_exists() {{ printf %s "$2"; return 0; }}; '
        f"reply_defers_on_unreachable_fix {shlex.quote(reply_body)} example/example"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
    ).stdout


# --- defer decision -------------------------------------------------------


def test_unpushed_fix_claim_defers():
    # The race: a real backticked SHA the compare API cannot yet reach.
    assert _defers("Fixed in `115dbb3`. `step` now returns a boolean.", exists=False)


def test_fix_already_pushed_verifies_now():
    # Same claim, but the fix commit is now in the repo: verify, don't defer.
    assert not _defers("Fixed in `115dbb3`.", exists=True)


def test_rebased_fix_commit_verifies_now():
    # The commit exists but a later rebase moved the fix onto an equivalent SHA,
    # so it is no longer an ancestor of HEAD. Its fix content is still in HEAD, so
    # verify against HEAD rather than deferring forever on a diverged commit.
    assert not _defers("Fixed in `115dbb3`.", exists=True)


def test_reply_with_no_claimed_commit_verifies_now():
    # A plain acknowledgment names no commit, so there is nothing to wait for;
    # commit_exists is stubbed absent yet the thread still verifies.
    assert not _defers("Thanks, good catch.", exists=False)


# --- extraction precision (the permanent-defer guard) ---------------------


def test_prose_hex_is_not_a_claim():
    # An unbackticked hex word is not a commit reference; matching it would defer
    # a legitimate thread on every cycle. It must fall through to verify-now.
    assert not _defers("Handled the deadbeef case in the parser.", exists=False)


def test_backticked_non_hex_is_not_a_claim():
    assert not _defers("Refactored `step(dir)` to return a boolean.", exists=False)


def test_short_hex_color_is_not_a_claim():
    assert not _defers("Set the background to `#fff` now.", exists=False)


def test_unbackticked_sha_falls_through():
    # No delimiter to anchor on: bias to the safe pre-existing behavior.
    assert not _defers("Fixed in commit 115dbb3 (no backticks).", exists=False)


# --- what gets extracted --------------------------------------------------


def test_extracts_backticked_sha():
    assert _claimed_sha("Fixed in `115dbb3`.") == "115dbb3"


def test_extracts_from_link_form():
    body = "Fixed in [`115dbb3:L72-L80`](https://github.com/x/y/blob/115dbb3/f#L72)"
    assert _claimed_sha(body) == "115dbb3"


def test_extracts_from_commit_url():
    body = "see https://github.com/x/y/commit/deadbeef123456 for the fix"
    assert _claimed_sha(body) == "deadbeef123456"


def test_extracted_sha_is_lowercased():
    # The commit lookup takes lowercase hex; normalize so a capitalized claim
    # still resolves.
    assert _claimed_sha("Done in `A1B2C3D`.") == "a1b2c3d"


def test_no_claim_passes_empty_sha():
    assert _claimed_sha("Thanks!") == ""
