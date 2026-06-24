"""Tests for the Status comment reviewed-SHAs trail (daemon/status_trail.py, ADR 0021).

The trail is the one Status-comment element that owns state: a folded `<details>`
block of every reviewed HEAD SHA, accumulated across ticks by reading the prior
rows back out of the comment body. These tests pin the block-scoped parse (it
ignores findings-index list items), the skip-if-present idempotency policy, the
purity of `merge`, the singular/plural count, and the round-trip (a rendered block
re-parses to the same rows) the accumulation relies on.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))

import status_trail as st  # noqa: E402

# A realistic Status comment body carrying a two-row trail, plus a findings-index
# list item above it whose shape must NOT be mistaken for a trail row.
BODY_WITH_TRAIL = """✅ Reviewed `2c2d8dd1`

_Scope: `767548c..2c2d8dd`_

**3 findings · 1 open · 2 resolved**

- [`src/foo.tsx:42`](https://example/c1) · open

<details><summary>1 file</summary>

- `src/foo.tsx`

</details>

<details><summary>Reviewed 2 commits</summary>

- `a1b2c3d0` · 2026-06-24 11:18 UTC
- `d4cabf6d` · 2026-06-24 12:47 UTC

</details>

🤖 _pr-review-agent_

<!-- pr-review-agent:status -->
"""


# --- parse_entries --------------------------------------------------------


def test_parse_reads_only_trail_block_rows():
    # The findings-index `- [\`src/foo.tsx:42\`]…` item sits above the trail; the
    # block fence keeps it out, and the path token is not pure hex anyway.
    assert st.parse_entries(BODY_WITH_TRAIL) == [
        ("a1b2c3d0", "2026-06-24 11:18 UTC"),
        ("d4cabf6d", "2026-06-24 12:47 UTC"),
    ]


def test_parse_empty_when_no_block():
    assert st.parse_entries("✅ Reviewed `abc`\n\n🤖 _pr-review-agent_") == []
    assert st.parse_entries("") == []


# --- merge: idempotency policy --------------------------------------------


def test_merge_appends_a_new_sha():
    prior = [("a1b2c3d0", "t1")]
    assert st.merge(prior, "d4cabf6d", "t2") == [
        ("a1b2c3d0", "t1"),
        ("d4cabf6d", "t2"),
    ]


def test_merge_skips_a_sha_already_present():
    # Re-review of an unchanged HEAD / retry: no new row, original time kept.
    prior = [("a1b2c3d0", "first-seen"), ("d4cabf6d", "t2")]
    assert st.merge(prior, "a1b2c3d0", "later-time") == prior


def test_merge_without_add_returns_entries_unchanged():
    prior = [("a1b2c3d0", "t1")]
    assert st.merge(prior, None, None) == prior


def test_merge_is_pure():
    prior = [("a1b2c3d0", "t1")]
    st.merge(prior, "d4cabf6d", "t2")
    assert prior == [("a1b2c3d0", "t1")]  # input untouched


# --- render ---------------------------------------------------------------


def test_render_empty_when_no_rows_and_no_add():
    assert st.render("") == ""


def test_render_singular_noun_for_one_commit():
    out = st.render("", add_sha="a1b2c3d0", add_time="2026-06-24 11:18 UTC")
    assert "<summary>Reviewed 1 commit</summary>" in out
    assert "- `a1b2c3d0` · 2026-06-24 11:18 UTC" in out


def test_render_plural_noun_and_count():
    out = st.render(BODY_WITH_TRAIL, add_sha="ffffffff", add_time="2026-06-24 15:00 UTC")
    assert "<summary>Reviewed 3 commits</summary>" in out


def test_render_round_trips_through_parse():
    # The accumulation reads its own prior output back: render → parse must be
    # lossless so a SHA written this tick survives into the next.
    out = st.render("", add_sha="a1b2c3d0", add_time="2026-06-24 11:18 UTC")
    assert st.parse_entries(out) == [("a1b2c3d0", "2026-06-24 11:18 UTC")]


def test_accumulation_over_three_ticks():
    body = ""
    for sha, when in [("aaaaaaaa", "t1"), ("bbbbbbbb", "t2"), ("aaaaaaaa", "t3")]:
        body = st.render(body, add_sha=sha, add_time=when)
    # Third tick re-sees aaaaaaaa: skipped, so two distinct rows, original times.
    assert st.parse_entries(body) == [("aaaaaaaa", "t1"), ("bbbbbbbb", "t2")]
    assert "<summary>Reviewed 2 commits</summary>" in body
