"""Tests for deferring a reply thread whose claimed fix commit is not in HEAD.

The reply race (sa#163): the operator commits a fix, replies naming that SHA,
and pushes, but the reply pass fetched HEAD before the push landed. Verifying
the pre-fix code pushes back on a correct fix. `reply_defers_on_unreachable_fix`
defers such a thread so a later cycle verifies it once the push arrives.

The predicate has two halves: extract the commit the reply claims (the precision
risk, since a false-positive hex would defer a legitimate thread forever), and
gate the defer on ancestry via `is_fast_forward`. Reachability itself goes
through the compare API and is covered by live dogfood (see test_diff_scope);
here `is_fast_forward` is stubbed so the test drives extraction and the defer
decision without a git repo or the network. Stubbing it to always-unreachable
ties the defer outcome directly to "did we extract a real SHA," so a spurious
extraction surfaces as a wrong defer.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "daemon" / "lib.sh"


def _defers(reply_body: str, *, reachable: bool) -> bool:
    """True when the thread defers. `reachable` stubs is_fast_forward's verdict."""
    ff_rc = 0 if reachable else 1
    script = (
        f"source {LIB}; "
        f"is_fast_forward() {{ return {ff_rc}; }}; "
        f"reply_defers_on_unreachable_fix {shlex.quote(reply_body)} example/example deadbeefcafe"
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
    """The SHA passed to is_fast_forward (lowercased), or "" when none is claimed."""
    script = (
        f"source {LIB}; "
        f'is_fast_forward() {{ printf %s "$2"; return 0; }}; '
        f"reply_defers_on_unreachable_fix {shlex.quote(reply_body)} example/example deadbeefcafe"
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
    assert _defers("Fixed in `115dbb3`. `step` now returns a boolean.", reachable=False)


def test_fix_already_in_head_verifies_now():
    # Same claim, but the fix commit is an ancestor of HEAD: verify, don't defer.
    assert not _defers("Fixed in `115dbb3`.", reachable=True)


def test_reply_with_no_claimed_commit_verifies_now():
    # A plain acknowledgment names no commit, so there is nothing to wait for;
    # is_fast_forward is stubbed unreachable yet the thread still verifies.
    assert not _defers("Thanks, good catch.", reachable=False)


# --- extraction precision (the permanent-defer guard) ---------------------


def test_prose_hex_is_not_a_claim():
    # An unbackticked hex word is not a commit reference; matching it would defer
    # a legitimate thread on every cycle. It must fall through to verify-now.
    assert not _defers("Handled the deadbeef case in the parser.", reachable=False)


def test_backticked_non_hex_is_not_a_claim():
    assert not _defers("Refactored `step(dir)` to return a boolean.", reachable=False)


def test_short_hex_color_is_not_a_claim():
    assert not _defers("Set the background to `#fff` now.", reachable=False)


def test_unbackticked_sha_falls_through():
    # No delimiter to anchor on: bias to the safe pre-existing behavior.
    assert not _defers("Fixed in commit 115dbb3 (no backticks).", reachable=False)


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
    # is_fast_forward compares against HEAD_OID (lowercase hex); normalize so a
    # capitalized claim still matches.
    assert _claimed_sha("Done in `A1B2C3D`.") == "a1b2c3d"


def test_no_claim_passes_empty_sha():
    assert _claimed_sha("Thanks!") == ""
