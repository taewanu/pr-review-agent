"""Integration harness for daemon/poll.sh dispatch + dedup (#33).

poll.sh's per-PR loop is the wiring that decides, for every open PR, whether to
dispatch a review, whether to reply, and what `--last-sha` to scope to. None of
that was covered: the unit tests exercise `discover_sentinel_sha`, `state_*`,
and the config loader in isolation, but the branch logic gluing them together
(eligibility skips, sentinel-vs-state fallback, the ADR 0006 rc=2 skip, the
same-SHA short-circuit, state written only on success) ran only in production.

This harness runs the real poll.sh over a faithful fake daemon dir: real
poll.sh, lib.sh, and load_config.py, with stub `gh`, `review-pr.sh`, and
`reply-pr.sh`. The stubs record their invocations so each test asserts on what
poll.sh dispatched and with which arguments, plus the state files it wrote.

It also pins the contract #92 (bounded-parallel dispatch) must preserve: same
PRs dispatched, same `--last-sha`, same state writes, same skip logging.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app_auth_fixture import BOT_LOGIN_REST, install_app_stubs

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON = REPO_ROOT / "daemon"

OWNER_REPO = "example/example"
SHA_HEAD = "a" * 40
SHA_PRIOR = "b" * 40


def _pr(
    number: int,
    *,
    head: str = SHA_HEAD,
    draft: bool = False,
    author: str = "contributor",
    labels: list[str] | None = None,
) -> dict:
    return {
        "number": number,
        "headRefOid": head,
        "isDraft": draft,
        "author": {"login": author},
        "labels": [{"name": name} for name in (labels or [])],
        "url": f"https://github.com/{OWNER_REPO}/pull/{number}",
    }


def _sentinel_review(sha: str, *, login: str = BOT_LOGIN_REST) -> dict:
    body = f"summary\n\n<!-- pr-review-agent:sha:{sha} -->"
    return {
        "user": {"login": login},
        "body": body,
        "submitted_at": "2026-06-01T10:00:00Z",
        "created_at": "2026-06-01T09:55:00Z",
    }


@dataclass
class PollResult:
    review_calls: list[str]
    reply_calls: list[str]
    state: dict[int, dict]
    stderr: str
    peak_concurrency: int = 0

    def reviewed(self, pr: int) -> bool:
        return any(f"/pull/{pr}" in call for call in self.review_calls)

    def replied(self, pr: int) -> bool:
        return any(f"/pull/{pr}" in call for call in self.reply_calls)

    def last_sha_for(self, pr: int) -> str | None:
        """The --last-sha review-pr.sh was dispatched with, or None if absent."""
        for call in self.review_calls:
            if f"/pull/{pr}" not in call:
                continue
            parts = call.split()
            if "--last-sha" in parts:
                return parts[parts.index("--last-sha") + 1]
            return ""  # dispatched without --last-sha (first review)
        return None


_STUB_GH = """#!/usr/bin/env bash
data="{data}"
args="$*"
case "$args" in
  "auth status"*) exit 0 ;;
  "repo view"*) echo '{{"viewerPermission":"WRITE"}}'; exit 0 ;;
  "pr list"*) cat "$data/pr_list.json"; exit 0 ;;
  *"/pulls/"*"/reviews"*)
    [[ "$args" =~ /pulls/([0-9]+)/reviews ]] && n="${{BASH_REMATCH[1]}}"
    [[ -f "$data/reviews_${{n}}.FAIL" ]] && {{ echo "boom" >&2; exit 1; }}
    f="$data/reviews_${{n}}.json"; [[ -f "$f" ]] && cat "$f" || echo '[]'
    exit 0 ;;
  *"/issues/"*"/comments"*)
    echo '[]'; exit 0 ;;
  *) echo "stub gh: unexpected args: $args" >&2; exit 99 ;;
esac
"""

# Logs argv, then exits 1 only when a per-PR fail marker exists, so a test can
# drive the "review failed, state untouched" branch for one specific PR.
_STUB_CHILD = """#!/usr/bin/env bash
echo "$*" >> "{log}"
url="${{@: -1}}"
[[ "$url" =~ /pull/([0-9]+) ]] && n="${{BASH_REMATCH[1]}}"
[[ -f "{faildir}/fail_${{n}}" ]] && exit 1
exit 0
"""

# Review stub: like _STUB_CHILD, but also brackets its run with start/end events
# and an optional sleep. Replaying the append-ordered events gives the true peak
# concurrency without relying on wall-clock timestamps; the sleep keeps a review
# alive long enough for the next dispatch to overlap it when the cap allows.
_STUB_REVIEW = """#!/usr/bin/env bash
echo "$*" >> "{log}"
url="${{@: -1}}"
[[ "$url" =~ /pull/([0-9]+) ]] && n="${{BASH_REMATCH[1]}}"
echo "start $n" >> "{events}"
sleep {sleep}
echo "end $n" >> "{events}"
[[ -f "{faildir}/fail_${{n}}" ]] && exit 1
exit 0
"""


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_poll(
    tmp_path: Path,
    prs: list[dict],
    *,
    reviews: dict[int, list[dict]] | None = None,
    reviews_fail: set[int] | None = None,
    state_seed: dict[int, str] | None = None,
    review_fail: set[int] | None = None,
    review_own_prs: bool = True,
    opt_out_label: str = "no-ai-review",
    github_user: str = "operator",
    max_parallel: int = 1,
    review_sleep: float = 0,
) -> PollResult:
    root = tmp_path / "root"
    daemon = root / "daemon"
    data = tmp_path / "ghdata"
    bindir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    for d in (daemon, data, bindir, state_dir):
        d.mkdir(parents=True)

    # Real scripts under test; everything else around them is a stub.
    for name in ("poll.sh", "lib.sh", "load_config.py"):
        shutil.copy(DAEMON / name, daemon / name)

    (root / ".env").write_text(
        f"REPOS={OWNER_REPO}\n"
        f"GITHUB_USER={github_user}\n"
        "GITHUB_APP_ID=4361858\n"
        f"REVIEW_OWN_PRS={'true' if review_own_prs else 'false'}\n"
        f"OPT_OUT_LABEL={opt_out_label}\n"
        f"MAX_PARALLEL={max_parallel}\n"
    )

    (data / "pr_list.json").write_text(json.dumps(prs))
    for number, payload in (reviews or {}).items():
        (data / f"reviews_{number}.json").write_text(json.dumps(payload))
    for number in reviews_fail or set():
        (data / f"reviews_{number}.FAIL").write_text("")

    for number, sha in (state_seed or {}).items():
        owner, repo = OWNER_REPO.split("/")
        body = {"last_reviewed_sha": sha, "review_id": 0, "ts_iso": "2026-06-01T00:00:00Z"}
        (state_dir / f"{owner}-{repo}-{number}.json").write_text(json.dumps(body))

    review_log = tmp_path / "review.log"
    reply_log = tmp_path / "reply.log"
    events = tmp_path / "review_events.log"
    faildir = tmp_path / "faildir"
    faildir.mkdir()
    for number in review_fail or set():
        (faildir / f"fail_{number}").write_text("")

    _executable(bindir / "gh", _STUB_GH.format(data=data))
    _executable(
        daemon / "review-pr.sh",
        _STUB_REVIEW.format(log=review_log, events=events, sleep=review_sleep, faildir=faildir),
    )
    _executable(daemon / "reply-pr.sh", _STUB_CHILD.format(log=reply_log, faildir=faildir))
    app_env = install_app_stubs(bindir)

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env.update(app_env)
    env["PR_REVIEW_STATE_DIR"] = str(state_dir)
    proc = subprocess.run(
        ["bash", str(daemon / "poll.sh")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"poll.sh failed:\n{proc.stderr}"

    state: dict[int, dict] = {}
    owner, repo = OWNER_REPO.split("/")
    for number in (pr["number"] for pr in prs):
        path = state_dir / f"{owner}-{repo}-{number}.json"
        if path.exists():
            state[number] = json.loads(path.read_text())

    # Peak concurrency by replaying start/end in append order: a start that
    # lands before a prior end means those reviews overlapped. Order-based, so
    # no dependence on wall-clock granularity.
    peak = current = 0
    if events.exists():
        for line in events.read_text().splitlines():
            if line.startswith("start"):
                current += 1
                peak = max(peak, current)
            elif line.startswith("end"):
                current -= 1

    return PollResult(
        review_calls=review_log.read_text().splitlines() if review_log.exists() else [],
        reply_calls=reply_log.read_text().splitlines() if reply_log.exists() else [],
        state=state,
        stderr=proc.stderr,
        peak_concurrency=peak,
    )


# --- eligibility skips -------------------------------------------------------


def test_draft_pr_is_skipped_entirely(tmp_path):
    res = _run_poll(tmp_path, [_pr(1, draft=True)])
    assert not res.reviewed(1)
    assert not res.replied(1)
    assert "skipped (draft)" in res.stderr


def test_opt_out_label_skips_the_pr(tmp_path):
    res = _run_poll(tmp_path, [_pr(1, labels=["no-ai-review"])])
    assert not res.reviewed(1)
    assert "opt-out label" in res.stderr


# --- first review / dedup ----------------------------------------------------


def test_first_review_dispatches_without_last_sha(tmp_path):
    # No sentinel, no state: a first review reads the full PR diff.
    res = _run_poll(tmp_path, [_pr(1)], reviews={1: []})
    assert res.reviewed(1)
    assert res.last_sha_for(1) == ""  # dispatched, no --last-sha
    assert res.state[1]["last_reviewed_sha"] == SHA_HEAD


def test_same_sha_sentinel_skips_review_but_still_replies(tmp_path):
    # The sentinel reports HEAD as already reviewed: no re-review, but the reply
    # path runs every cycle independent of dedup.
    res = _run_poll(tmp_path, [_pr(1, head=SHA_HEAD)], reviews={1: [_sentinel_review(SHA_HEAD)]})
    assert not res.reviewed(1)
    assert res.replied(1)
    assert "same SHA" in res.stderr


def test_advanced_sentinel_rereviews_scoped_to_prior_sha(tmp_path):
    # Sentinel reports an older SHA than HEAD: re-review, scoped to that SHA.
    res = _run_poll(tmp_path, [_pr(1, head=SHA_HEAD)], reviews={1: [_sentinel_review(SHA_PRIOR)]})
    assert res.reviewed(1)
    assert res.last_sha_for(1) == SHA_PRIOR


def test_state_fallback_same_sha_skips(tmp_path):
    # APIs return no sentinel, but the state file records HEAD as reviewed.
    res = _run_poll(tmp_path, [_pr(1, head=SHA_HEAD)], reviews={1: []}, state_seed={1: SHA_HEAD})
    assert not res.reviewed(1)
    assert "same SHA" in res.stderr


# --- discovery API failure (ADR 0006 rc=2) -----------------------------------


def test_discovery_failure_with_no_state_skips_the_pr(tmp_path):
    # rc=2 + empty state must skip, not collapse to a first review. The reply
    # path still runs (it precedes dedup).
    res = _run_poll(tmp_path, [_pr(1)], reviews_fail={1})
    assert not res.reviewed(1)
    assert res.replied(1)
    assert "discovery API down" in res.stderr
    assert 1 not in res.state


def test_discovery_failure_falls_back_to_state(tmp_path):
    # rc=2 but a state file exists: re-review scoped to the stored SHA rather
    # than skipping.
    res = _run_poll(tmp_path, [_pr(1, head=SHA_HEAD)], reviews_fail={1}, state_seed={1: SHA_PRIOR})
    assert res.reviewed(1)
    assert res.last_sha_for(1) == SHA_PRIOR


# --- state write semantics ---------------------------------------------------


def test_failed_review_leaves_state_untouched(tmp_path):
    # A review that exits non-zero must not advance state, so the next cycle
    # retries instead of treating the PR as reviewed.
    res = _run_poll(
        tmp_path,
        [_pr(1, head=SHA_HEAD)],
        reviews={1: [_sentinel_review(SHA_PRIOR)]},
        state_seed={1: SHA_PRIOR},
        review_fail={1},
    )
    assert res.reviewed(1)  # dispatched
    assert res.state[1]["last_reviewed_sha"] == SHA_PRIOR  # but state unchanged
    assert "review failed" in res.stderr


# --- multiple PRs in one cycle -----------------------------------------------


def test_each_pr_dispatched_independently(tmp_path):
    # One cycle, three PRs in different states: first-review, same-SHA skip, and
    # an advanced-sentinel re-review. Each resolves on its own.
    res = _run_poll(
        tmp_path,
        [_pr(1, head=SHA_HEAD), _pr(2, head=SHA_HEAD), _pr(3, head=SHA_HEAD)],
        reviews={
            1: [],
            2: [_sentinel_review(SHA_HEAD)],
            3: [_sentinel_review(SHA_PRIOR)],
        },
    )
    assert res.last_sha_for(1) == ""
    assert not res.reviewed(2)
    assert res.last_sha_for(3) == SHA_PRIOR


# --- bounded-parallel dispatch (#92) -----------------------------------------


def test_default_dispatch_is_serial(tmp_path):
    # MAX_PARALLEL=1 (the default) reviews one PR at a time: no two overlap.
    res = _run_poll(
        tmp_path,
        [_pr(n) for n in (1, 2, 3)],
        reviews={1: [], 2: [], 3: []},
        review_sleep=0.4,
        max_parallel=1,
    )
    assert res.peak_concurrency == 1
    assert all(res.reviewed(n) for n in (1, 2, 3))


def test_parallel_dispatch_runs_up_to_the_cap(tmp_path):
    # MAX_PARALLEL=2 with four PRs: reviews overlap, but never more than two at
    # once. peak == 2 proves both that it parallelises and that it stays bounded.
    res = _run_poll(
        tmp_path,
        [_pr(n) for n in (1, 2, 3, 4)],
        reviews={n: [] for n in (1, 2, 3, 4)},
        review_sleep=0.4,
        max_parallel=2,
    )
    assert res.peak_concurrency == 2
    assert all(res.reviewed(n) for n in (1, 2, 3, 4))
    # Every PR's review still lands and advances its own state.
    assert all(res.state[n]["last_reviewed_sha"] == SHA_HEAD for n in (1, 2, 3, 4))


def test_parallel_dispatch_preserves_per_pr_scoping(tmp_path):
    # The contract #33 pinned must survive parallelism: each PR keeps its own
    # --last-sha (first-review vs advanced-sentinel re-review) when dispatched
    # concurrently.
    res = _run_poll(
        tmp_path,
        [_pr(1, head=SHA_HEAD), _pr(2, head=SHA_HEAD)],
        reviews={1: [], 2: [_sentinel_review(SHA_PRIOR)]},
        review_sleep=0.3,
        max_parallel=2,
    )
    assert res.last_sha_for(1) == ""
    assert res.last_sha_for(2) == SHA_PRIOR
