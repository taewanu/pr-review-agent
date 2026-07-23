"""Idle-cycle collapse in daemon/poll.sh (#165).

A polling cycle is idle when it touches no PR. When the prior cycle was also
idle, poll.sh suppresses the repeated preamble. On a non-TTY (a log file, or the
captured pipe here) consecutive idle cycles emit nothing and only bump a
persistent streak counter in the state dir; the first idle still prints one
closing line. A cycle that does work clears the streak. The in-place TTY redraw
of the "(×N)" line is not exercised here (no pseudo-terminal).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from app_auth_fixture import install_app_stubs

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON = REPO_ROOT / "daemon"
OWNER_REPO = "example/example"

# Stub gh: auth/repo-access ok, `pr list` serves the file the test rewrites
# between cycles, everything else (reviews, comments, compare) is empty.
_GH_STUB = """#!/usr/bin/env bash
case "$*" in
  "auth status"*) exit 0 ;;
  "repo view"*) echo '{"viewerPermission":"WRITE"}'; exit 0 ;;
  "pr list"*) cat "%s"; exit 0 ;;
  *) echo '[]'; exit 0 ;;
esac
"""


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _setup(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    daemon = tmp_path / "daemon"
    bindir = tmp_path / "bin"
    state = tmp_path / "state"
    for d in (daemon, bindir, state):
        d.mkdir(parents=True)
    for name in ("poll.sh", "lib.sh", "load_config.py"):
        (daemon / name).write_bytes((DAEMON / name).read_bytes())
    (tmp_path / ".env").write_text(
        f"REPOS={OWNER_REPO}\n"
        "GITHUB_USER=operator\n"
        "GITHUB_APP_ID=4361858\n"
        "REVIEW_OWN_PRS=true\n"
        "OPT_OUT_LABEL=no-ai-review\n"
        "MAX_PARALLEL=1\n"
    )
    pr_list = tmp_path / "pr_list.json"
    pr_list.write_text("[]")
    _executable(bindir / "gh", _GH_STUB % pr_list)
    install_app_stubs(bindir)
    # Stub child scripts so a working cycle needs no real review/reply.
    for name in ("review-pr.sh", "reply-pr.sh"):
        _executable(daemon / name, "#!/usr/bin/env bash\nexit 0\n")
    return daemon, bindir, state, pr_list


def _poll(daemon: Path, bindir: Path, state: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["APP_KEY_PATH"] = str(bindir / "app.pem")
    env["PR_REVIEW_STATE_DIR"] = str(state)
    return subprocess.run(
        ["bash", str(daemon / "poll.sh")],
        capture_output=True,
        text=True,
        env=env,
    )


def test_first_idle_prints_then_consecutive_idles_go_silent(tmp_path):
    daemon, bindir, state, _ = _setup(tmp_path)

    r1 = _poll(daemon, bindir, state)
    assert r1.returncode == 0, r1.stderr
    assert "no open PRs" in r1.stderr
    assert "cycle done · no open PRs" in r1.stderr

    r2 = _poll(daemon, bindir, state)
    assert r2.returncode == 0, r2.stderr
    assert r2.stderr.strip() == "", f"2nd idle cycle should be silent: {r2.stderr!r}"

    r3 = _poll(daemon, bindir, state)
    assert r3.stderr.strip() == ""
    assert (state / "idle.count").read_text().strip() == "3"


def test_working_cycle_clears_the_idle_streak(tmp_path):
    daemon, bindir, state, pr_list = _setup(tmp_path)

    _poll(daemon, bindir, state)  # idle 1
    _poll(daemon, bindir, state)  # idle 2
    assert (state / "idle.count").exists()

    pr_list.write_text(
        json.dumps(
            [
                {
                    "number": 1,
                    "headRefOid": "a" * 40,
                    "isDraft": False,
                    "author": {"login": "contributor"},
                    "labels": [],
                    "url": f"https://github.com/{OWNER_REPO}/pull/1",
                }
            ]
        )
    )
    r = _poll(daemon, bindir, state)
    assert r.returncode == 0, r.stderr
    assert not (state / "idle.count").exists(), "a working cycle must clear the streak"
    assert "cycle done" in r.stderr
