"""Tests for the verdict refresh a reply pass runs after it resolves threads.

The review's verdict is derived from the PR's open finding threads (ADR 0040
decision 4) and shown on two surfaces: the status comment's head-line and index,
and the `review` check (ADR 0039). A reply pass changes that input when it
resolves a thread, so both surfaces must follow, derived from one state so they
cannot disagree. `refresh_review_verdict` (lib.sh) is driven here against a stub
`gh` that serves the PR's comments, threads, and check runs from files and
records every write, and reply-pr.sh is driven end to end over a copy of the
daemon whose agent and poster are stubs.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from app_auth_fixture import install_app_stubs

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON = REPO_ROOT / "daemon"
LIB = DAEMON / "lib.sh"
APP_STUB = REPO_ROOT / "tests" / "lib_app_stub.sh"

sys.path.insert(0, str(DAEMON))
import findings_index  # noqa: E402

# The canonical slug, because only its head-line names the verdict ("you shall
# pass" / "you shall not pass"); every other slug renders one plain line for both.
SLUG = "youshallnotmerge"
BOT_REST = f"{SLUG}[bot]"
REPO_URL = "https://github.com/example/example"
PR_URL = f"{REPO_URL}/pull/7"
REVIEWED = "a" * 40
NEWER = "b" * 40
STATUS_ID = 900
RUN_ID = 4242
APP_ID = 4361858
PASS_LINE = "you shall pass"
BLOCK_LINE = "you shall not pass"


def _thread(n: int, resolved: bool) -> dict:
    """One bot finding thread in the GraphQL shape both reviewThreads queries read."""
    return {
        "id": f"PRRT_{n}",
        "isResolved": resolved,
        "path": f"src/f{n}.py",
        "originalLine": 10 + n,
        "originalStartLine": None,
        "line": 10 + n,
        "startLine": None,
        "comments": {
            "nodes": [
                {
                    "databaseId": 100 + n,
                    "id": f"PRRC_{n}",
                    "url": f"{PR_URL}#discussion_r{100 + n}",
                    "author": {"login": SLUG},
                    "body": f"finding {n}",
                }
            ]
        },
    }


def _graphql(threads: list[dict]) -> dict:
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": threads}}}}}


def _flat(threads: list[dict]) -> list[dict]:
    """The fetch_open_review_threads shape findings_index reads."""
    return [
        {
            "thread_id": t["id"],
            "is_resolved": t["isResolved"],
            "root_author": SLUG,
            "root_comment_url": t["comments"]["nodes"][0]["url"],
            "path": t["path"],
            "original_line": t["originalLine"],
        }
        for t in threads
    ]


def _bash(snippet: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"source {LIB}; source {APP_STUB}; {snippet}"],
        capture_output=True,
        text=True,
        env=env or os.environ.copy(),
    )


def _status_body(
    threads: list[dict], sha: str | None, tmp: Path, *, with_index: bool = True
) -> str:
    """The status comment the review posted, rendered by the review's own helpers:
    head-line from the open-thread verdict, the index with a delta line and an
    outside-the-diff pointer (none when `with_index` is false, as when the
    review's thread read failed), the trail, and the sentinel when `sha` is given."""
    threads_file = tmp / "review-threads.json"
    threads_file.write_text(json.dumps(_flat(threads)))
    state = "block" if any(not t["isResolved"] for t in threads) else "pass"
    index_cmd = (
        f'index="$(python3 {DAEMON}/findings_index.py --threads {threads_file} '
        f'--operator {SLUG} --advisory 1 --review-url {PR_URL}#review --new 1 --fixed 0)"; '
        if with_index
        else 'index=""; '
    )
    script = index_cmd + (
        f'trail="$(printf "" | python3 {DAEMON}/status_trail.py --add-sha {REVIEWED[:7]} '
        '--add-time "2026-11-13 09:00 UTC")"; '
        f'render_status_comment "$(render_status_headline {SLUG} {state} '
        f'"$(status_sha_link {REPO_URL} {REVIEWED})" {REVIEWED})" "the scope" 1 src/f1.py '
        f'"$index" "$trail" "{sha or ""}"'
    )
    result = _bash(script)
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n")


# The run lookup is served as GitHub would: only the fixture's runs on the URL's
# commit that match the query's check_name and app_id, so a query that drops a
# parameter is answered with runs it must not touch.
_STUB_GH = """#!/usr/bin/env bash
data="@DATA@"
args="$*"
printf '%s\\n' "$args" >>"$data/calls.log"
param() {
  local v="${query#*&$1=}"
  [[ "$v" != "$query" ]] && printf '%s' "${v%%&*}"
}
case "$args" in
  "pr view"*) cat "$data/meta.json" ;;
  "repo clone"*) mkdir -p "$4" ;;
  *"-X PATCH"*"/issues/comments/"*)
    [[ -f "$data/comment_patch.FAIL" ]] && exit 1
    for a in "$@"; do
      [[ "$a" == body=* ]] && printf '%s' "${a#body=}" >"$data/comment_patch.txt"
    done
    ;;
  *"-X PATCH"*"/check-runs/"*)
    [[ -f "$data/checks.FAIL" || -f "$data/check_patch.FAIL" ]] && exit 1
    for a in "$@"; do
      [[ "$a" == */check-runs/* ]] && printf '%s' "${a##*/}" >"$data/check_patch_id"
    done
    cat >"$data/check_patch.json"
    ;;
  *"/commits/"*"/check-runs"*)
    if [[ -f "$data/checks.FAIL" ]]; then
      echo "HTTP 403: Resource not accessible by integration" >&2
      exit 1
    fi
    sha="${2#*/commits/}"
    sha="${sha%%/*}"
    query="&${2#*\\?}&"
    jq --arg sha "$sha" --arg name "$(param check_name)" --arg app "$(param app_id)" \\
      '{check_runs: [.[] | select(.head_sha == $sha and .name == $name
        and (.app.id | tostring) == $app)]}' "$data/check_runs.json" >"$data/served.json"
    while [[ $# -gt 0 && "$1" != "--jq" ]]; do shift; done
    jq -r "$2" "$data/served.json"
    ;;
  *"/issues/"*"/comments"*) cat "$data/issue_comments.json" ;;
  *"/pulls/"*"/comments"*) cat "$data/review_comments.json" ;;
  *graphql*)
    if [[ -f "$data/resolved_by_reply" ]]; then
      cat "$data/graphql_after.json"
    else
      cat "$data/graphql.json"
    fi
    ;;
  *) echo "stub gh: unexpected args: $args" >&2; exit 99 ;;
esac
"""


@dataclass
class Fixture:
    root: Path
    data: Path
    bin: Path
    env: dict

    def calls(self) -> str:
        log = self.data / "calls.log"
        return log.read_text() if log.exists() else ""

    def comment_patch(self) -> str | None:
        f = self.data / "comment_patch.txt"
        return f.read_text() if f.exists() else None

    def check_patch(self) -> dict | None:
        f = self.data / "check_patch.json"
        return json.loads(f.read_text()) if f.exists() else None

    def check_patch_id(self) -> int | None:
        f = self.data / "check_patch_id"
        return int(f.read_text()) if f.exists() else None

    def set_runs(self, runs: list[dict]) -> None:
        (self.data / "check_runs.json").write_text(json.dumps(runs))

    def fail(self, write: str) -> None:
        (self.data / f"{write}.FAIL").touch()


def _run(run_id: int, status: str, conclusion: str | None, **overrides) -> dict:
    """One check run as GitHub lists it, by default the App's `review` run on REVIEWED."""
    return {
        "id": run_id,
        "name": "review",
        "head_sha": REVIEWED,
        "app": {"id": APP_ID},
        "status": status,
        "conclusion": conclusion,
        **overrides,
    }


def _fixture(
    tmp_path: Path,
    *,
    at_review: list[dict],
    now: list[dict],
    shown_conclusion: str,
    sentinel: str | None = REVIEWED,
    checks_fail: bool = False,
    body: str | None = None,
) -> Fixture:
    """A PR whose review of REVIEWED left the threads `at_review`, its status
    comment and check showing that verdict, and whose threads are `now` when the
    refresh reads them. `body` replaces the rendered status comment."""
    data = tmp_path / "data"
    data.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_STUB_GH.replace("@DATA@", str(data)))
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env.update(install_app_stubs(bindir))
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["PR_REVIEW_STATE_DIR"] = str(tmp_path / "state")
    env["STATUS_EDIT_RETRY_SLEEP_SECONDS"] = "0"
    env["CHECK_RUN_RETRY_SLEEP_SECONDS"] = "0"

    if body is None:
        body = _status_body(at_review, sentinel, tmp_path)
    (data / "issue_comments.json").write_text(
        json.dumps(
            [
                {"id": 1, "user": {"login": "someone"}, "body": "lgtm"},
                {"id": STATUS_ID, "user": {"login": BOT_REST}, "body": body},
            ]
        )
    )
    (data / "graphql.json").write_text(json.dumps(_graphql(now)))
    fx = Fixture(tmp_path, data, bindir, env)
    fx.set_runs([_run(RUN_ID, "completed", shown_conclusion)])
    if checks_fail:
        fx.fail("checks")
    return fx


def _refresh(fx: Fixture, head: str = REVIEWED) -> subprocess.CompletedProcess:
    env = dict(fx.env, PRA_BOT_LOGIN_REST=BOT_REST, PRA_BOT_LOGIN_GQL=SLUG)
    return _bash(
        f"PRA_APP_ID={APP_ID}; refresh_review_verdict example example 7 {head} {REPO_URL}", env
    )


# --- resolved to pass --------------------------------------------------------


def test_resolving_the_last_open_thread_turns_both_surfaces_to_pass(tmp_path):
    fx = _fixture(
        tmp_path,
        at_review=[_thread(1, False)],
        now=[_thread(1, True)],
        shown_conclusion="failure",
    )
    result = _refresh(fx)
    assert result.returncode == 0, result.stderr

    body = fx.comment_patch()
    assert body is not None
    assert body.splitlines()[0].endswith(PASS_LINE)
    assert "**1 finding · 0 open · 1 resolved**" in body
    assert "src/f1.py:11`](" in body and body.count("· resolved") == 1

    check = fx.check_patch()
    assert check is not None
    assert check["conclusion"] == "success"
    assert check["output"]["title"] == "No findings open on this commit"
    assert f"check-runs/{RUN_ID}" in fx.calls()


def test_the_refresh_keeps_what_describes_the_review_itself(tmp_path):
    # Scope, the per-push delta, the outside-the-diff pointer, the trail, and the
    # sentinel record the review, not the threads, so a refresh must not lose them.
    fx = _fixture(
        tmp_path, at_review=[_thread(1, False)], now=[_thread(1, True)], shown_conclusion="failure"
    )
    _refresh(fx)
    body = fx.comment_patch()
    assert body is not None
    for kept in (
        "_Scope: the scope_",
        "_+1 new · +1 advisory_",
        f"_+ 1 outside the diff → [review]({PR_URL}#review)_",
        f"- `{REVIEWED[:7]}` · 2026-11-13 09:00 UTC",
        f"<!-- pr-review-agent:sha:{REVIEWED} -->",
        "<!-- pr-review-agent:status -->",
    ):
        assert kept in body


def test_a_thread_still_open_keeps_the_verdict_and_the_check(tmp_path):
    fx = _fixture(
        tmp_path,
        at_review=[_thread(1, False), _thread(2, False)],
        now=[_thread(1, True), _thread(2, False)],
        shown_conclusion="failure",
    )
    assert _refresh(fx).returncode == 0
    body = fx.comment_patch()
    assert body is not None
    assert body.splitlines()[0].endswith(BLOCK_LINE)
    assert "**2 findings · 1 open · 1 resolved**" in body
    assert fx.check_patch() is None


# --- reopened to block -------------------------------------------------------


def test_a_reopened_thread_turns_both_surfaces_to_block(tmp_path):
    fx = _fixture(
        tmp_path,
        at_review=[_thread(1, True)],
        now=[_thread(1, False)],
        shown_conclusion="success",
    )
    assert _refresh(fx).returncode == 0
    body = fx.comment_patch()
    assert body is not None
    assert body.splitlines()[0].endswith(BLOCK_LINE)
    assert "**1 finding · 1 open · 0 resolved**" in body
    check = fx.check_patch()
    assert check is not None
    assert check["conclusion"] == "failure"
    assert check["output"]["title"] == "Findings open on this commit"


def test_both_surfaces_render_from_one_state(tmp_path):
    # Every transition lands the head-line and the check on the same verdict: a
    # head-line saying pass beside a failing check (or the reverse) is the
    # disagreement ADR 0039 decision 3 rules out.
    cases = [
        ([_thread(1, False)], [_thread(1, True)], "failure"),
        ([_thread(1, True)], [_thread(1, False)], "success"),
        ([_thread(1, False), _thread(2, False)], [_thread(1, True), _thread(2, True)], "failure"),
    ]
    for i, (at_review, now, shown) in enumerate(cases):
        case_dir = tmp_path / str(i)
        case_dir.mkdir()
        fx = _fixture(case_dir, at_review=at_review, now=now, shown_conclusion=shown)
        assert _refresh(fx).returncode == 0
        body, check = fx.comment_patch(), fx.check_patch()
        assert body is not None and check is not None
        assert body.splitlines()[0].endswith(PASS_LINE) == (check["conclusion"] == "success")


# --- which commit ------------------------------------------------------------


def test_head_moved_past_the_reviewed_commit_leaves_both_surfaces_to_the_next_review(tmp_path):
    fx = _fixture(
        tmp_path, at_review=[_thread(1, False)], now=[_thread(1, True)], shown_conclusion="failure"
    )
    result = _refresh(fx, head=NEWER)
    assert result.returncode == 0
    assert fx.comment_patch() is None
    assert fx.check_patch() is None
    assert "graphql" not in fx.calls()


def test_a_status_comment_without_a_completed_review_is_left_alone(tmp_path):
    # The Reviewing and failed renders carry no sentinel: a review is running or
    # failed, and the next one to finish writes both surfaces.
    fx = _fixture(
        tmp_path,
        at_review=[_thread(1, False)],
        now=[_thread(1, True)],
        shown_conclusion="failure",
        sentinel=None,
    )
    assert _refresh(fx).returncode == 0
    assert fx.comment_patch() is None
    assert fx.check_patch() is None


def test_unchanged_threads_write_nothing_and_cost_three_reads(tmp_path):
    # When both surfaces already show the verdict, the refresh reads the status
    # comment, the threads, and the run, and writes nothing.
    fx = _fixture(
        tmp_path, at_review=[_thread(1, False)], now=[_thread(1, False)], shown_conclusion="failure"
    )
    result = _refresh(fx)
    assert result.returncode == 0
    assert fx.comment_patch() is None
    assert fx.check_patch() is None
    calls = [c for c in fx.calls().splitlines() if c.startswith("api ")]
    assert len(calls) == 3, calls
    assert not any("PATCH" in c for c in calls)
    assert "already shows block" in result.stderr
    assert f"check run {RUN_ID} already failure" in result.stderr


@pytest.mark.parametrize("stale", ["failure", "neutral"])
def test_a_current_comment_beside_a_stale_check_gets_the_check_repaired(tmp_path, stale):
    # The comment edit landed on an earlier pass and the check write did not, or
    # the review stopped between its comment edit and its conclude and left the
    # run neutral: the comment's sentinel says the review finished, so the run is
    # brought to the verdict the threads give.
    fx = _fixture(
        tmp_path, at_review=[_thread(1, True)], now=[_thread(1, True)], shown_conclusion=stale
    )
    result = _refresh(fx)
    assert result.returncode == 0
    assert fx.comment_patch() is None
    check = fx.check_patch()
    assert check is not None and check["conclusion"] == "success"
    assert f"check run {RUN_ID} updated from {stale} to success" in result.stderr


def test_the_run_lookup_names_the_commit_the_check_and_the_app(tmp_path):
    fx = _fixture(
        tmp_path, at_review=[_thread(1, False)], now=[_thread(1, True)], shown_conclusion="failure"
    )
    _refresh(fx)
    lookup = next(c for c in fx.calls().splitlines() if "/commits/" in c)
    url = lookup.split()[1]
    path, _, query = url.partition("?")
    assert path == f"repos/example/example/commits/{REVIEWED}/check-runs"
    params = dict(p.split("=", 1) for p in query.split("&"))
    assert params["check_name"] == "review"
    assert params["app_id"] == str(APP_ID)
    assert params["filter"] == "all"


def test_of_several_review_runs_on_the_commit_the_newest_is_updated(tmp_path):
    # The newest of the App's `review` runs on the commit is the one the latest
    # review of it opened; an older attempt's run, another App's run, another
    # check, and a run on another commit are none of the refresh's business.
    fx = _fixture(
        tmp_path, at_review=[_thread(1, False)], now=[_thread(1, True)], shown_conclusion="failure"
    )
    fx.set_runs(
        [
            _run(10, "completed", "neutral"),
            _run(20, "completed", "failure"),
            _run(30, "completed", "failure", app={"id": 999}),
            _run(40, "completed", "failure", name="lint"),
            _run(50, "completed", "failure", head_sha=NEWER),
        ]
    )
    assert _refresh(fx).returncode == 0
    assert fx.check_patch_id() == 20
    check = fx.check_patch()
    assert check is not None and check["conclusion"] == "success"


def test_a_review_in_flight_owns_both_surfaces(tmp_path):
    fx = _fixture(
        tmp_path, at_review=[_thread(1, False)], now=[_thread(1, True)], shown_conclusion="failure"
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "example-example-7.lock").write_text(f"{os.getpid()} {int(time.time())}\n")
    result = _refresh(fx)
    assert result.returncode == 0
    assert fx.calls() == ""
    # The lock is the review's: skipping must not release it.
    assert (state / "example-example-7.lock").exists()


def test_the_refresh_releases_the_review_lock(tmp_path):
    fx = _fixture(
        tmp_path, at_review=[_thread(1, False)], now=[_thread(1, True)], shown_conclusion="failure"
    )
    assert _refresh(fx).returncode == 0
    assert not (tmp_path / "state" / "example-example-7.lock").exists()


# --- degradation -------------------------------------------------------------


def test_no_checks_permission_still_refreshes_the_comment(tmp_path):
    fx = _fixture(
        tmp_path,
        at_review=[_thread(1, False)],
        now=[_thread(1, True)],
        shown_conclusion="failure",
        checks_fail=True,
    )
    result = _refresh(fx)
    assert result.returncode == 0
    body = fx.comment_patch()
    assert body is not None and body.splitlines()[0].endswith(PASS_LINE)
    assert fx.check_patch() is None
    assert "check run lookup" in result.stderr and "failed" in result.stderr
    assert "no review check run" not in result.stderr


def test_a_failed_comment_edit_keeps_the_check_from_running_ahead(tmp_path):
    # The check never shows a verdict the status comment does not; the next pass
    # finds both stale and brings both.
    fx = _fixture(
        tmp_path, at_review=[_thread(1, False)], now=[_thread(1, True)], shown_conclusion="failure"
    )
    fx.fail("comment_patch")
    result = _refresh(fx)
    assert result.returncode == 0
    assert fx.check_patch() is None
    assert "status comment not refreshed" in result.stderr
    assert "refreshed to" not in result.stderr


def test_a_failed_check_update_is_reported_as_failed(tmp_path):
    fx = _fixture(
        tmp_path, at_review=[_thread(1, False)], now=[_thread(1, True)], shown_conclusion="failure"
    )
    fx.fail("check_patch")
    result = _refresh(fx)
    assert result.returncode == 0
    assert f"check run {RUN_ID} not updated; it stays failure" in result.stderr
    assert "updated from" not in result.stderr


@pytest.mark.parametrize(
    "runs",
    [
        [_run(RUN_ID, "in_progress", None)],
        [_run(10, "completed", "failure"), _run(20, "in_progress", None)],
    ],
)
def test_an_in_progress_run_is_never_concluded_by_the_refresh(tmp_path, runs):
    # ADR 0039 decision 4: the review that opened a run is the one that ends it,
    # and an older completed run is not the one the checks row now reports.
    fx = _fixture(
        tmp_path, at_review=[_thread(1, False)], now=[_thread(1, True)], shown_conclusion="failure"
    )
    fx.set_runs(runs)
    assert _refresh(fx).returncode == 0
    assert fx.check_patch() is None


def test_a_comment_from_a_review_that_could_not_read_the_threads_gets_its_index(tmp_path):
    # A review whose own thread read failed renders no index, and its head-line
    # may say pass over an open finding; the refresh adds the index the head-line
    # rests on and brings both surfaces to the threads.
    fx = _fixture(
        tmp_path,
        at_review=[],
        now=[_thread(1, False)],
        shown_conclusion="success",
        body=_status_body([], REVIEWED, tmp_path, with_index=False),
    )
    assert _refresh(fx).returncode == 0
    body = fx.comment_patch()
    assert body is not None
    assert body.splitlines()[0].endswith(BLOCK_LINE)
    assert "_Scope: the scope_\n\n**1 finding · 1 open · 0 resolved**" in body
    check = fx.check_patch()
    assert check is not None and check["conclusion"] == "failure"


def test_a_comment_in_a_form_the_refresh_does_not_rewrite_is_named_and_left(tmp_path):
    # A body with the sentinel but neither a rollup nor a scope line to put one under.
    body = f"edited\n\n<!-- pr-review-agent:sha:{REVIEWED} -->\n\n<!-- pr-review-agent:status -->"
    fx = _fixture(
        tmp_path,
        at_review=[_thread(1, False)],
        now=[_thread(1, True)],
        shown_conclusion="failure",
        body=body,
    )
    result = _refresh(fx)
    assert result.returncode == 0
    assert "does not rewrite" in result.stderr
    assert "already" not in result.stderr
    assert fx.comment_patch() is None and fx.check_patch() is None


def test_a_crlf_body_that_matches_the_threads_is_not_rewritten(tmp_path):
    # GitHub can return a body with CRLF line endings; the comparison must not
    # count line endings as a difference, or every pass re-edits the comment.
    fx = _fixture(
        tmp_path, at_review=[_thread(1, True)], now=[_thread(1, True)], shown_conclusion="success"
    )
    comments = json.loads((fx.data / "issue_comments.json").read_text())
    comments[1]["body"] = comments[1]["body"].replace("\n", "\r\n")
    (fx.data / "issue_comments.json").write_text(json.dumps(comments))
    result = _refresh(fx)
    assert result.returncode == 0
    assert fx.comment_patch() is None
    assert "already shows pass" in result.stderr


# --- refresh_body ------------------------------------------------------------


def test_refresh_body_puts_the_rollup_under_the_scope_when_the_body_has_none():
    body = "🛑 x: you shall not pass\n\n_Scope: s_\n\n<!-- pr-review-agent:status -->"
    threads = _flat([_thread(1, True)])
    refreshed = findings_index.refresh_body(body, threads, SLUG, "new head-line")
    assert refreshed is not None
    assert refreshed.startswith(
        "new head-line\n\n_Scope: s_\n\n**1 finding · 0 open · 1 resolved**"
    )
    assert refreshed.endswith("<!-- pr-review-agent:status -->")


def test_refresh_body_returns_none_for_a_body_it_cannot_bring_to_the_threads():
    threads = _flat([_thread(1, True)])
    no_scope = "head\n\n<!-- pr-review-agent:status -->"
    assert findings_index.refresh_body(no_scope, threads, SLUG, "new") is None
    stale_rollup = "head\n\n**1 finding · 1 open · 0 resolved**\n\n- [`a:1`](u) · open"
    assert findings_index.refresh_body(stale_rollup, [], SLUG, "new") is None


# --- reply-pr.sh end to end ---------------------------------------------------

# Answers the version preflight as the oldest claude the daemon accepts, then
# stands in for the reply agent.
_FAKE_CLAUDE = """#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
  echo '2.1.281 (Claude Code)'
  exit 0
fi
cat >/dev/null
printf '%s\\n' '{"type": "result", "result": "replies", "total_cost_usd": 0}'
"""

# Stands in for create_reply.py: records that the reply posted and resolves the
# thread, the way the real poster's resolveReviewThread does.
_FAKE_POSTER = """import os, pathlib, sys
data = pathlib.Path(os.environ["FAKE_DATA"])
(data / "reply_posted").touch()
(data / "resolved_by_reply").touch()
sys.exit(int(os.environ.get("FAKE_POST_RC", "0")))
"""


def _reply_pass(
    fx: Fixture, post_rc: int = 0, *, with_reply: bool = True
) -> subprocess.CompletedProcess:
    root = fx.root / "checkout"
    shutil.copytree(DAEMON, root / "daemon", ignore=shutil.ignore_patterns("__pycache__"))
    (root / ".claude").symlink_to(REPO_ROOT / ".claude")
    (root / "daemon" / "create_reply.py").write_text(_FAKE_POSTER)
    for name, text in (("claude", _FAKE_CLAUDE), ("git", "#!/usr/bin/env bash\nexit 0\n")):
        stub = fx.bin / name
        stub.write_text(text)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    finding = {
        "id": 101,
        "in_reply_to_id": None,
        "user": {"login": BOT_REST, "type": "Bot"},
        "body": "finding 1",
        "path": "src/f1.py",
        "line": 11,
    }
    reply = {
        "id": 202,
        "in_reply_to_id": 101,
        "user": {"login": "author", "type": "User"},
        "body": "Fixed, the guard is in place now.",
    }
    comments = [finding, reply] if with_reply else [finding]
    (fx.data / "review_comments.json").write_text(json.dumps(comments))
    (fx.data / "graphql_after.json").write_text(json.dumps(_graphql([_thread(1, True)])))
    (fx.data / "meta.json").write_text(
        json.dumps(
            {
                "headRepositoryOwner": {"login": "example"},
                "headRepository": {"name": "example"},
                "headRefName": "fix",
                "headRefOid": REVIEWED,
            }
        )
    )
    env = dict(fx.env, FAKE_DATA=str(fx.data), FAKE_POST_RC=str(post_rc))
    return subprocess.run(
        ["bash", str(root / "daemon" / "reply-pr.sh"), "--app-id", str(APP_ID), PR_URL],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_reply_pass_that_resolves_the_last_thread_passes_both_surfaces(tmp_path):
    fx = _fixture(
        tmp_path, at_review=[_thread(1, False)], now=[_thread(1, False)], shown_conclusion="failure"
    )
    _use_canonical_slug(fx)
    result = _reply_pass(fx)
    assert result.returncode == 0, result.stderr
    assert (fx.data / "reply_posted").exists()
    body = fx.comment_patch()
    assert body is not None and body.splitlines()[0].endswith(PASS_LINE)
    check = fx.check_patch()
    assert check is not None and check["conclusion"] == "success"


def test_reply_pass_without_checks_permission_still_succeeds(tmp_path):
    fx = _fixture(
        tmp_path,
        at_review=[_thread(1, False)],
        now=[_thread(1, False)],
        shown_conclusion="failure",
        checks_fail=True,
    )
    _use_canonical_slug(fx)
    result = _reply_pass(fx)
    assert result.returncode == 0, result.stderr
    assert (fx.data / "reply_posted").exists()
    assert fx.check_patch() is None


def test_reply_pass_refreshes_even_when_posting_fails_partway(tmp_path):
    # A poster that resolved a thread and then failed leaves that thread resolved;
    # the surfaces follow it, and the pass still reports its failure.
    fx = _fixture(
        tmp_path, at_review=[_thread(1, False)], now=[_thread(1, False)], shown_conclusion="failure"
    )
    _use_canonical_slug(fx)
    result = _reply_pass(fx, post_rc=1)
    assert result.returncode == 1
    body = fx.comment_patch()
    assert body is not None and body.splitlines()[0].endswith(PASS_LINE)


def test_reply_pass_with_no_reply_makes_no_refresh_call(tmp_path):
    # The refresh costs a polling cycle nothing unless a reply was answered.
    fx = _fixture(
        tmp_path, at_review=[_thread(1, False)], now=[_thread(1, False)], shown_conclusion="failure"
    )
    _use_canonical_slug(fx)
    result = _reply_pass(fx, with_reply=False)
    assert result.returncode == 0, result.stderr
    assert not (fx.data / "reply_posted").exists()
    calls = fx.calls()
    assert "/issues/7/comments" not in calls
    assert "graphql" not in calls
    assert "check-runs" not in calls


def _use_canonical_slug(fx: Fixture) -> None:
    """Point the curl stub's installation probe at the canonical slug, so the bot
    logins reply-pr.sh derives match the status comment's author."""
    curl = fx.bin / "curl"
    curl.write_text(curl.read_text().replace('"app_slug": "example"', f'"app_slug": "{SLUG}"'))
