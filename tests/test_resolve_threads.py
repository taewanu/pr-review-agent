"""Tests for daemon/resolve_threads.py — commit-driven resolution (#125, ADR 0017)."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_PATH = REPO_ROOT / "daemon" / "resolve_threads.py"
LIB_SH = REPO_ROOT / "daemon" / "lib.sh"
_spec = importlib.util.spec_from_file_location("resolve_threads", RESOLVE_PATH)
assert _spec is not None and _spec.loader is not None
resolve_threads = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve_threads)

IncrementDiff = resolve_threads.IncrementDiff
select_candidates = resolve_threads.select_candidates
select_retry_threads = resolve_threads.select_retry_threads
parse_verdict = resolve_threads.parse_verdict
MARKER = resolve_threads.PROVENANCE_MARKER
RESOLVED_SENTINEL = resolve_threads.RESOLUTION_SENTINEL


# ---------- IncrementDiff.parse / touched (OLD side) ----------


def test_old_side_single_hunk():
    diff = IncrementDiff.parse(
        textwrap.dedent(
            """\
            diff --git a/daemon/lib.sh b/daemon/lib.sh
            index abc..def 100644
            --- a/daemon/lib.sh
            +++ b/daemon/lib.sh
            @@ -10,3 +10,4 @@ foo()
                 x=1
            -    y=2
            +    y=3
            +    z=4
            """
        )
    )
    # Old side covers lines 10..12 (start 10, count 3).
    assert diff.touched("daemon/lib.sh", 10)
    assert diff.touched("daemon/lib.sh", 12)
    assert not diff.touched("daemon/lib.sh", 9)
    assert not diff.touched("daemon/lib.sh", 13)
    assert not diff.touched("other.sh", 10)


def test_pure_insertion_has_no_old_side_lines():
    # `@@ -41,0 +42,3 @@` is a pure insertion: nothing on the old side is touched.
    diff = IncrementDiff.parse(
        textwrap.dedent(
            """\
            diff --git a/f.py b/f.py
            @@ -41,0 +42,3 @@
            +a
            +b
            +c
            """
        )
    )
    assert diff.ranges["f.py"] == []
    assert not diff.touched("f.py", 41)
    assert not diff.touched("f.py", 42)


def test_old_count_defaults_to_one():
    diff = IncrementDiff.parse(
        textwrap.dedent(
            """\
            diff --git a/f.py b/f.py
            @@ -7 +7,2 @@
             keep
            +added
            """
        )
    )
    assert diff.touched("f.py", 7)
    assert not diff.touched("f.py", 8)


def test_rename_registers_both_paths():
    diff = IncrementDiff.parse(
        textwrap.dedent(
            """\
            diff --git a/old/name.py b/new/name.py
            @@ -3,2 +3,2 @@
            -old
            +new
            """
        )
    )
    assert diff.touched("old/name.py", 3)
    assert diff.touched("new/name.py", 3)


def test_touched_range_overlap():
    diff = IncrementDiff.parse(
        textwrap.dedent(
            """\
            diff --git a/f.py b/f.py
            @@ -20,2 +20,2 @@
            -a
            +b
            """
        )
    )
    # A multi-line finding [15..21] overlaps the touched range [20..21].
    assert diff.touched("f.py", 21, start_line=15)
    # A finding entirely above the touched range does not.
    assert not diff.touched("f.py", 18, start_line=15)


# ---------- select_candidates ----------


def _diff_touching(path: str, start: int, count: int) -> IncrementDiff:
    body = "".join(f"-line{i}\n" for i in range(count))
    return IncrementDiff.parse(
        f"diff --git a/{path} b/{path}\n@@ -{start},{count} +{start},0 @@\n{body}"
    )


def _thread(**over) -> dict:
    base = {
        "thread_id": "PRRT_1",
        "is_resolved": False,
        "root_author": "operator",
        "root_body": f"Unbounded loop.\n\n{MARKER}",
        "path": "daemon/lib.sh",
        "original_line": 10,
        "original_start_line": None,
        "has_resolution_stamp": False,
    }
    base.update(over)
    return base


DIFF = _diff_touching("daemon/lib.sh", 10, 3)  # touches lines 10..12


def test_candidate_happy_path():
    out = select_candidates([_thread()], DIFF, "operator")
    assert out == [
        {
            "thread_id": "PRRT_1",
            "path": "daemon/lib.sh",
            "line": 10,
            "finding_body": f"Unbounded loop.\n\n{MARKER}",
        }
    ]


def test_resolved_thread_excluded():
    assert select_candidates([_thread(is_resolved=True)], DIFF, "operator") == []


def test_non_operator_author_excluded():
    assert select_candidates([_thread(root_author="someone-else")], DIFF, "operator") == []


def test_missing_provenance_marker_excluded():
    assert (
        select_candidates([_thread(root_body="A human's manual comment")], DIFF, "operator") == []
    )


def test_line_not_touched_excluded():
    assert select_candidates([_thread(original_line=99)], DIFF, "operator") == []


def test_null_original_line_excluded():
    assert select_candidates([_thread(original_line=None)], DIFF, "operator") == []


def test_all_open_bypasses_diff_filter():
    # original_line 99 is untouched, but all_open takes every open daemon thread.
    out = select_candidates([_thread(original_line=99)], DIFF, "operator", all_open=True)
    assert len(out) == 1 and out[0]["thread_id"] == "PRRT_1"


def test_all_open_still_enforces_open_and_ownership():
    threads = [
        _thread(thread_id="PRRT_resolved", is_resolved=True),
        _thread(thread_id="PRRT_human", root_author="someone-else"),
        _thread(thread_id="PRRT_keep", original_line=99),
    ]
    out = select_candidates(threads, DIFF, "operator", all_open=True)
    assert [c["thread_id"] for c in out] == ["PRRT_keep"]


# ---------- parse_verdict (safe-biased) ----------


def _fence(body: str) -> str:
    return f"Some reasoning here.\n\n```json\n{body}\n```\n"


def test_verdict_fixed_true():
    assert parse_verdict(_fence('{"fixed": true, "rationale": "loop now breaks on a cap"}')) == {
        "fixed": True,
        "rationale": "loop now breaks on a cap",
    }


def test_verdict_fixed_false():
    assert (
        parse_verdict(_fence('{"fixed": false, "rationale": "still one function"}'))["fixed"]
        is False
    )


def test_verdict_no_fence_leaves_open():
    assert parse_verdict("no fence at all")["fixed"] is False


def test_verdict_bad_json_leaves_open():
    assert parse_verdict(_fence("{not json}"))["fixed"] is False


def test_verdict_missing_fixed_leaves_open():
    assert parse_verdict(_fence('{"rationale": "x"}'))["fixed"] is False


def test_verdict_non_bool_fixed_leaves_open():
    assert parse_verdict(_fence('{"fixed": "yes", "rationale": "x"}'))["fixed"] is False


def test_verdict_empty_rationale_leaves_open():
    assert parse_verdict(_fence('{"fixed": true, "rationale": "  "}'))["fixed"] is False


def test_verdict_last_fence_wins():
    raw = _fence('{"fixed": true, "rationale": "first"}') + _fence(
        '{"fixed": false, "rationale": "second"}'
    )
    assert parse_verdict(raw) == {"fixed": False, "rationale": "second"}


# ---------- Slice B: has_resolution_stamp routing (candidate vs retry) ----------


def test_noted_thread_excluded_from_candidates():
    # An already-noted open thread is never re-judged: it would otherwise get a
    # second note (and a redundant agent call) every time a later commit touches it.
    assert select_candidates([_thread(has_resolution_stamp=True)], DIFF, "operator") == []


def test_all_open_excludes_noted_thread():
    # The force-push path drops the diff filter but must still skip noted threads,
    # else a rebase would re-note every prior fix.
    out = select_candidates([_thread(has_resolution_stamp=True)], DIFF, "operator", all_open=True)
    assert out == []


def test_retry_selects_open_noted_daemon_thread():
    assert select_retry_threads([_thread(has_resolution_stamp=True)], "operator") == [
        {"thread_id": "PRRT_1"}
    ]


def test_retry_excludes_unnoted_thread():
    # No note yet -> nothing to retry; it flows through the judge path instead.
    assert select_retry_threads([_thread(has_resolution_stamp=False)], "operator") == []


def test_retry_excludes_resolved_thread():
    assert (
        select_retry_threads([_thread(has_resolution_stamp=True, is_resolved=True)], "operator")
        == []
    )


def test_retry_excludes_non_daemon_thread():
    assert (
        select_retry_threads(
            [_thread(has_resolution_stamp=True, root_author="someone")], "operator"
        )
        == []
    )
    assert (
        select_retry_threads(
            [_thread(has_resolution_stamp=True, root_body="manual comment")], "operator"
        )
        == []
    )


def test_candidate_and_retry_are_disjoint():
    # The whole point of has_resolution_stamp: one thread is either a judge candidate or a
    # retry, never both. A touched-but-unnoted one judges; a noted one retries.
    threads = [
        _thread(thread_id="PRRT_judge", has_resolution_stamp=False),
        _thread(thread_id="PRRT_retry", has_resolution_stamp=True),
    ]
    judged = {c["thread_id"] for c in select_candidates(threads, DIFF, "operator")}
    retried = {r["thread_id"] for r in select_retry_threads(threads, "operator")}
    assert judged == {"PRRT_judge"}
    assert retried == {"PRRT_retry"}
    assert judged.isdisjoint(retried)


# ---------- #159 / ADR 0019: Resolution stamp (appended to the Finding comment) ----------


def test_resolution_stamp_shape():
    # The stamp is appended to the Finding's own comment, so it carries no
    # Provenance marker (the host comment already has one). One visible line
    # (lead + commit-anchored link + rationale), then the hidden dedup sentinel.
    stamp = resolve_threads.build_resolution_stamp(
        "loop now breaks on a cap", "[`abc1234:L10`](https://x/blob/abc/a#L10)"
    )
    assert resolve_threads.PROVENANCE_MARKER not in stamp
    assert stamp.endswith(resolve_threads.RESOLUTION_SENTINEL)
    assert stamp.split("\n\n") == [
        "✅ _Resolved in_ [`abc1234:L10`](https://x/blob/abc/a#L10): loop now breaks on a cap",
        resolve_threads.RESOLUTION_SENTINEL,
    ]


def test_resolution_stamp_without_link():
    # No head sha or no line -> the lead drops its "in" clause, rationale stays.
    stamp = resolve_threads.build_resolution_stamp("defect gone after the rewrite", None)
    assert stamp.split("\n\n") == [
        "✅ _Resolved_: defect gone after the rewrite",
        resolve_threads.RESOLUTION_SENTINEL,
    ]


# ---------- Slice B: voice gating leaves a thread open ----------


def _act_dry_run(notes: list[dict], retry: list[dict]) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        nf = Path(tmp) / "notes.json"
        rf = Path(tmp) / "retry.json"
        nf.write_text(json.dumps(notes))
        rf.write_text(json.dumps(retry))
        out = subprocess.run(
            [
                "python3",
                str(RESOLVE_PATH),
                "act",
                "--notes",
                str(nf),
                "--retry",
                str(rf),
                "--head-owner",
                "o",
                "--head-repo",
                "r",
                "--head-sha",
                "abcdef1234567890",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(out.stdout)


def test_act_dry_run_plan_stamps_and_resolves():
    plan = _act_dry_run(
        [
            {
                "thread_id": "PRRT_a",
                "comment_id": "RC_a",
                "path": "a.py",
                "line": 10,
                "rationale": "loop now caps",
            }
        ],
        [{"thread_id": "PRRT_b"}],
    )
    assert [n["thread_id"] for n in plan["would_stamp"]] == ["PRRT_a"]
    # Both the freshly-stamped thread and the retry thread are resolved.
    assert plan["would_resolve"] == ["PRRT_a", "PRRT_b"]
    assert plan["skipped"] == []


def test_act_dry_run_voice_violation_leaves_open():
    # An em dash in the rationale fails the voice gate, so the stamp is not built and
    # the thread is left open (safe bias): it is neither stamped nor resolved.
    plan = _act_dry_run(
        [{"thread_id": "PRRT_bad", "path": "a.py", "line": 10, "rationale": "fixed — see below"}],
        [],
    )
    assert plan["would_stamp"] == []
    assert plan["would_resolve"] == []
    assert len(plan["skipped"]) == 1 and "em dash" in plan["skipped"][0]


def test_act_dry_run_empty_rationale_leaves_open():
    plan = _act_dry_run(
        [{"thread_id": "PRRT_x", "path": "a.py", "line": 10, "rationale": "  "}], []
    )
    assert plan["would_stamp"] == [] and plan["would_resolve"] == []


# ---------- act stamps the Finding comment and resolves, through a gh stub ----------


def _run_act(
    notes: list[dict],
    retry: list[dict],
    *,
    fail_ops: list[str] | None = None,
):
    """Run `resolve_threads.py act` with a recording gh stub on PATH. Returns
    (result, calls): calls is a list of argv strings in order. update_review_comment
    and resolve_thread read only the returncode, so the stub needs no canned stdout;
    it can be made to fail only for calls whose argv contains a given substring."""
    fail_ops = fail_ops or []
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        (tmpd / "notes.json").write_text(json.dumps(notes))
        (tmpd / "retry.json").write_text(json.dumps(retry))
        argv_log = tmpd / "argv.txt"
        stub = tmpd / "gh"
        lines = [
            "#!/usr/bin/env bash",
            f'printf "%s\\0" "$*" >> "{argv_log}"',
        ]
        if fail_ops:
            clauses = " ".join(f'*"{op}"*) exit 1 ;;' for op in fail_ops)
            lines.append(f'case "$*" in {clauses} esac')
        lines.append("exit 0")
        stub.write_text("\n".join(lines) + "\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = os.environ.copy()
        env["PATH"] = f"{tmpd}:{env['PATH']}"
        argv = [
            "python3",
            str(RESOLVE_PATH),
            "act",
            "--notes",
            str(tmpd / "notes.json"),
            "--retry",
            str(tmpd / "retry.json"),
            "--head-owner",
            "o",
            "--head-repo",
            "r",
            "--head-sha",
            "abcdef1234567890",
        ]
        result = subprocess.run(argv, capture_output=True, text=True, env=env)
        calls = argv_log.read_text().split("\0")[:-1] if argv_log.exists() else []
        return result, calls


def test_act_stamps_comment_then_resolves():
    result, calls = _run_act(
        [
            {
                "thread_id": "PRRT_a",
                "comment_id": "RC_a",
                "finding_body": f"Unbounded loop.\n\n{MARKER}",
                "path": "a.py",
                "line": 10,
                "rationale": "loop now caps",
            }
        ],
        [{"thread_id": "PRRT_b"}],
    )
    assert result.returncode == 0, result.stderr
    joined = "\n".join(calls)
    # The stamp is a silent in-place edit, not a notifying review: no pending-review
    # ladder and no detached REST reply.
    assert "CreatePendingReview" not in joined
    assert "AddReply" not in joined
    assert "SubmitReview" not in joined
    assert "/replies" not in joined
    # The Finding's own comment is edited (its node id rides the UpdateComment call).
    update = [c for c in calls if "UpdateComment" in c]
    assert len(update) == 1 and "RC_a" in update[0]
    # Both the stamped thread and the retry thread are resolved.
    resolves = [c for c in calls if "resolveReviewThread" in c]
    assert any("PRRT_a" in c for c in resolves)
    assert any("PRRT_b" in c for c in resolves)


def test_act_resolve_only_for_retry_with_no_notes():
    # Retry-only tick: no notes, so no comment is edited, just the resolve retry.
    result, calls = _run_act([], [{"thread_id": "PRRT_b"}])
    assert result.returncode == 0, result.stderr
    joined = "\n".join(calls)
    assert "UpdateComment" not in joined
    assert any("resolveReviewThread" in c and "PRRT_b" in c for c in calls)


def test_act_update_failure_leaves_thread_open():
    # If the comment edit fails, the stamp never landed, so the thread must NOT be
    # resolved (safe bias): a later tick retries the edit.
    result, calls = _run_act(
        [
            {
                "thread_id": "PRRT_a",
                "comment_id": "RC_a",
                "finding_body": f"Unbounded loop.\n\n{MARKER}",
                "path": "a.py",
                "line": 10,
                "rationale": "loop now caps",
            }
        ],
        [],
        fail_ops=["UpdateComment"],
    )
    assert result.returncode == 0, result.stderr
    assert any("UpdateComment" in c for c in calls), "the edit was attempted"
    assert not any("resolveReviewThread" in c and "PRRT_a" in c for c in calls)


def test_act_already_stamped_skips_edit_still_resolves():
    # A re-run over a comment that already carries the stamp (its resolve had dropped):
    # append_stamp returns None, so no second edit, but the thread is still resolved.
    body = f"Unbounded loop.\n\n{MARKER}\n\n✅ _Resolved_: loop now caps\n\n{RESOLVED_SENTINEL}"
    result, calls = _run_act(
        [
            {
                "thread_id": "PRRT_a",
                "comment_id": "RC_a",
                "finding_body": body,
                "path": "a.py",
                "line": 10,
                "rationale": "loop now caps",
            }
        ],
        [],
    )
    assert result.returncode == 0, result.stderr
    assert "UpdateComment" not in "\n".join(calls), "an already-stamped comment is not re-edited"
    assert any("resolveReviewThread" in c and "PRRT_a" in c for c in calls)


# ---------- resolution sentinel pinned across the bash/Python boundary ----------


def test_resolution_sentinel_pinned_in_lib_sh():
    # lib.sh's fetch_open_review_threads scans the root comment for the same literal
    # to compute has_resolution_stamp; a drift would silently break retry detection.
    assert RESOLVED_SENTINEL in LIB_SH.read_text()
