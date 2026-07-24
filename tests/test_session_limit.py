"""Session-limit detection and the polling backoff it drives (#231).

A lens that hits the subscription quota prints one sentinel line and exits
having done no work, which looks exactly like all-lenses-failed from the
outside and means the opposite: an external quota with a known reset, not a
pipeline defect to debug. The two must never collide, so the category is
decided by the sentinel alone and never by the count of failed lenses.

The detection half lives in merge_findings.py, the pause file in lib.sh, and
the honouring of it in poll.sh; all three are exercised here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from app_auth_fixture import install_app_stubs

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON = REPO_ROOT / "daemon"
LIB = DAEMON / "lib.sh"
OWNER_REPO = "example/example"

_spec = importlib.util.spec_from_file_location("merge_findings", DAEMON / "merge_findings.py")
assert _spec is not None and _spec.loader is not None
merge_findings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_findings)

ExtractError = merge_findings.ExtractError

# The sentinel as the quota message renders it. Both apostrophe characters are
# represented, since which one reaches the log is not under our control.
SENTINEL = "You've hit your session limit · resets 5pm"
SENTINEL_CURLY = "You’ve hit your usage limit · resets 11:30pm"

# A genuine parse failure: the agent ran, wrote a real review, and produced no
# fenced payload. The half of the acceptance that proves the two don't collide.
PARSE_FAILURE = (
    "I reviewed the diff and found one issue worth raising.\n\n"
    "The retry loop in daemon/poll.sh has no ceiling, so a repo that is\n"
    "unreachable spins forever instead of backing off. I would bound it.\n\n"
    "Otherwise the change reads clean: the new helper is covered, the naming\n"
    "matches its neighbours, and nothing here touches the posting path.\n"
) * 4


def _wrap(payload: dict) -> str:
    return f"lens prose\n\n```json\n{json.dumps(payload)}\n```\n"


def _lens_with_finding() -> str:
    return _wrap(
        {
            "summary": "One issue found.",
            "comments": [
                {
                    "path": "daemon/poll.sh",
                    "line": 42,
                    "severity": "important",
                    "type": "bug",
                    "body": "**Unbounded retry.** The loop never backs off.",
                    "confidence": 90,
                }
            ],
        }
    )


# --- detector ----------------------------------------------------------------


def test_sentinel_with_a_reset_time_returns_it():
    assert merge_findings.session_limit_reset(SENTINEL) == "5pm"


def test_sentinel_without_a_readable_reset_returns_empty():
    assert merge_findings.session_limit_reset("You've hit your session limit") == ""


def test_non_sentinel_output_returns_none():
    assert merge_findings.session_limit_reset(PARSE_FAILURE) is None
    assert merge_findings.session_limit_reset("") is None


def test_sentinel_is_recognized_with_either_apostrophe_and_wording():
    assert merge_findings.session_limit_reset(SENTINEL_CURLY) == "11:30pm"


def test_the_phrase_quoted_inside_a_long_review_is_not_a_sentinel():
    # A finding that discusses the quota message must not pause the daemon.
    quoted = PARSE_FAILURE + "\nThe log then prints " + SENTINEL + "\n"
    assert merge_findings.session_limit_reset(quoted) is None


# --- category routing --------------------------------------------------------


def test_every_lens_limited_raises_session_limit():
    with pytest.raises(ExtractError) as exc:
        merge_findings.merge([SENTINEL] * 5)
    assert exc.value.category == merge_findings.SESSION_LIMIT_CATEGORY


def test_sentinel_free_all_lenses_failed_keeps_its_own_category():
    with pytest.raises(ExtractError) as exc:
        merge_findings.merge([PARSE_FAILURE] * 5)
    assert exc.value.category == "all-lenses-failed"


def test_one_limited_lens_among_parse_failures_is_not_a_session_limit():
    # The category comes from the sentinel on every raw, never from the count of
    # failures, so a real pipeline defect can never masquerade as a quota pause.
    with pytest.raises(ExtractError) as exc:
        merge_findings.merge([SENTINEL, PARSE_FAILURE, PARSE_FAILURE])
    assert exc.value.category == "all-lenses-failed"


def test_empty_payloads_with_a_limited_probe_raise_session_limit():
    # Orchestrator dispatch (#299): the roles write only their payload files,
    # so a quota hit leaves them empty and the sentinel lands only in the
    # orchestrator transcript. The probe carries the classification.
    with pytest.raises(ExtractError) as exc:
        merge_findings.merge(["", ""], session_limit_probe=SENTINEL)
    assert exc.value.category == merge_findings.SESSION_LIMIT_CATEGORY


def test_empty_payloads_with_a_sentinel_free_probe_stay_all_lenses_failed():
    # A real defect that broke every role must never masquerade as a quota
    # pause just because a probe was supplied.
    with pytest.raises(ExtractError) as exc:
        merge_findings.merge(["", ""], session_limit_probe="code: failed\nintent: failed")
    assert exc.value.category == "all-lenses-failed"


def test_a_parsed_payload_wins_over_a_limited_probe():
    # A role that landed before the wall is a degraded success, not a pause,
    # matching the mixed-run rule for per-role dispatch above.
    merged = merge_findings.merge([_lens_with_finding(), ""], session_limit_probe=SENTINEL)
    assert [c.line for c in merged.comments] == [42]


def test_partial_limit_stays_a_degraded_review():
    # Some lenses limited, one produced findings: process what came back rather
    # than pause, since the confidence gate already handles a short lens set.
    merged = merge_findings.merge([SENTINEL, SENTINEL, _lens_with_finding()])
    assert [c.line for c in merged.comments] == [42]


def _run_merge(tmp_path: Path, raw: str) -> subprocess.CompletedProcess[str]:
    """Run merge_findings.py over three lens files, the way review-pr.sh does."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(3):
        p = tmp_path / f".pr-review-raw-lens{i}.txt"
        p.write_text(raw)
        paths.append(str(p))
    return subprocess.run(
        [sys.executable, str(DAEMON / "merge_findings.py"), "--no-style", *paths],
        capture_output=True,
        text=True,
    )


def test_captured_session_limit_log_pauses_where_a_parse_failure_does_not(tmp_path: Path):
    # The collision the acceptance names: the same "no lens parsed" symptom must
    # route to different categories, and only the quota one carries a reset time
    # for review-pr.sh to pause on.
    limited = _run_merge(tmp_path / "limited", SENTINEL)
    broken = _run_merge(tmp_path / "broken", PARSE_FAILURE)
    assert limited.returncode == 1 and broken.returncode == 1
    assert "category=session-limit" in limited.stderr
    assert re.search(r"session_limit_deadline=\d+", limited.stderr)
    assert "category=all-lenses-failed" in broken.stderr
    assert "session_limit_deadline=" not in broken.stderr


def test_unreadable_reset_still_reports_the_category_with_an_empty_deadline(tmp_path: Path):
    r = _run_merge(tmp_path, "You've hit your session limit")
    assert "category=session-limit" in r.stderr
    assert "session_limit_deadline=\n" in r.stderr


def _run_merge_with_probe(tmp_path: Path, probe: str) -> subprocess.CompletedProcess[str]:
    """Run merge_findings.py the way the orchestrator dispatch does (#299):
    empty payload files plus --session-limit-probe on the transcript."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(2):
        p = tmp_path / f".pr-review-raw-role{i}.txt"
        p.write_text("")
        paths.append(str(p))
    probe_file = tmp_path / ".pr-review-orchestrator.txt"
    probe_file.write_text(probe)
    return subprocess.run(
        [
            sys.executable,
            str(DAEMON / "merge_findings.py"),
            "--no-style",
            "--session-limit-probe",
            str(probe_file),
            *paths,
        ],
        capture_output=True,
        text=True,
    )


def test_probe_flag_classifies_and_carries_the_probes_reset_time(tmp_path: Path):
    # The #231 pause contract under #299: empty role payloads plus a limited
    # transcript must produce both machine-readable stderr lines, with the
    # deadline read from the probe since the raws have no sentinel to offer.
    r = _run_merge_with_probe(tmp_path, SENTINEL)
    assert r.returncode == 1
    assert "category=session-limit" in r.stderr
    assert re.search(r"session_limit_deadline=\d+", r.stderr)


def test_a_long_transcript_around_the_sentinel_is_not_a_probe_match(tmp_path: Path):
    # The sentinel test caps its input length so quoted phrases in real text
    # never classify (see test_the_phrase_quoted_inside_a_long_review_is_not_a
    # _sentinel). The probe inherits that cap: a transcript that grew past it
    # around the sentinel degrades to all-lenses-failed, the safe direction
    # (a missed pause burns retries; a false pause silences the daemon).
    padded = ("orchestrator status line\n" * 20) + SENTINEL
    r = _run_merge_with_probe(tmp_path, padded)
    assert r.returncode == 1
    assert "category=all-lenses-failed" in r.stderr


# --- reset time to deadline ------------------------------------------------


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 21, hour, minute, 0)


@pytest.mark.parametrize(
    ("text", "expected_hour", "expected_minute"),
    [
        ("5pm", 17, 0),
        ("5 PM", 17, 0),
        ("3:30am", 3, 30),
        ("3:30 a.m.", 3, 30),
        ("23:15", 23, 15),
        ("12am", 0, 0),
        ("12pm", 12, 0),
    ],
)
def test_clock_forms_resolve(text: str, expected_hour: int, expected_minute: int):
    now = _at(1)
    deadline = merge_findings.session_limit_deadline(text, now)
    assert deadline is not None
    landed = datetime.fromtimestamp(deadline - 60)
    assert (landed.hour, landed.minute) == (expected_hour, expected_minute)


@pytest.mark.parametrize("text", ["", "soon", "later today", "25:00", "13pm"])
def test_unreadable_times_return_none_so_the_caller_falls_back(text: str):
    assert merge_findings.session_limit_deadline(text, _at(1)) is None


def test_a_reset_already_past_rolls_to_tomorrow():
    now = _at(18)
    deadline = merge_findings.session_limit_deadline("5pm", now)
    assert deadline is not None
    assert datetime.fromtimestamp(deadline - 60).day == now.day + 1


def test_the_deadline_carries_slack_past_the_reset_itself():
    # Resuming exactly at the reset races it; a minute of slack is deliberate.
    now = _at(1)
    deadline = merge_findings.session_limit_deadline("5pm", now)
    assert deadline == int(_at(17).timestamp()) + 60


# --- pause file --------------------------------------------------------------


def _sh(state_dir: Path, snippet: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PR_REVIEW_STATE_DIR"] = str(state_dir)
    return subprocess.run(
        ["bash", "-c", f"source {LIB}; {snippet}"],
        capture_output=True,
        text=True,
        env=env,
    )


def _pause_file(state_dir: Path) -> Path:
    return state_dir / "session-pause.epoch"


def test_pause_round_trip_stores_an_epoch_and_reads_back_active(tmp_path: Path):
    r = _sh(tmp_path, f"session_pause_write {int(time.time()) + 900}")
    assert r.returncode == 0
    written = r.stdout.strip()
    assert written.isdigit()
    assert _pause_file(tmp_path).read_text().strip() == written
    back = _sh(tmp_path, "session_pause_active")
    assert back.returncode == 0
    assert back.stdout.strip() == written


def test_an_absent_deadline_falls_back_to_the_bounded_interval(tmp_path: Path):
    r = _sh(tmp_path, 'session_pause_write ""')
    ahead = int(r.stdout.strip()) - int(time.time())
    assert 3500 < ahead <= 3600


def test_a_non_numeric_deadline_also_falls_back(tmp_path: Path):
    # merge_findings.py emits an empty value it could not resolve, but the shell
    # must not store junk it was handed either.
    r = _sh(tmp_path, 'session_pause_write "5pm"')
    ahead = int(r.stdout.strip()) - int(time.time())
    assert 3500 < ahead <= 3600


def test_the_fallback_interval_is_operator_overridable(tmp_path: Path):
    env = os.environ.copy()
    env["PR_REVIEW_STATE_DIR"] = str(tmp_path)
    env["SESSION_PAUSE_FALLBACK_SECONDS"] = "120"
    r = subprocess.run(
        ["bash", "-c", f'source {LIB}; session_pause_write ""'],
        capture_output=True,
        text=True,
        env=env,
    )
    ahead = int(r.stdout.strip()) - int(time.time())
    assert 60 < ahead <= 120


def test_a_resolved_deadline_is_stored_verbatim(tmp_path: Path):
    deadline = int(time.time()) + 4242
    r = _sh(tmp_path, f"session_pause_write {deadline}")
    assert int(r.stdout.strip()) == deadline


def test_passed_pause_reads_inactive_and_clears_the_file(tmp_path: Path):
    _pause_file(tmp_path).write_text("1\n")
    r = _sh(tmp_path, "session_pause_active")
    assert r.returncode == 1
    assert not _pause_file(tmp_path).exists()


def test_unreadable_pause_file_never_wedges_polling(tmp_path: Path):
    _pause_file(tmp_path).write_text("not-an-epoch\n")
    r = _sh(tmp_path, "session_pause_active")
    assert r.returncode == 1
    assert not _pause_file(tmp_path).exists()


def test_session_limit_has_an_author_facing_failure_reason():
    r = _sh(Path("/nonexistent"), "status_failure_reason session-limit")
    assert r.returncode == 0
    assert "quota" in r.stdout
    assert "—" not in r.stdout


# --- poll.sh honours the pause -----------------------------------------------


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _poll_setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    daemon = tmp_path / "daemon"
    bindir = tmp_path / "bin"
    state = tmp_path / "state"
    for d in (daemon, bindir, state):
        d.mkdir(parents=True)
    for name in ("poll.sh", "lib.sh", "load_config.py"):
        (daemon / name).write_bytes((DAEMON / name).read_bytes())
    (tmp_path / ".env").write_text(
        f"REPOS={OWNER_REPO}\nGITHUB_APP_ID=4361858\nOPT_OUT_LABEL=no-ai-review\nMAX_PARALLEL=1\n"
    )
    # A gh that records every invocation, so "no network call during a pause" is
    # asserted on evidence rather than on the absence of an error.
    _executable(
        bindir / "gh",
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >>"{tmp_path}/gh.calls"\n'
        'case "$*" in\n'
        '  "auth status"*) exit 0 ;;\n'
        '  "repo view"*) echo \'{"viewerPermission":"WRITE"}\'; exit 0 ;;\n'
        "  *) echo '[]'; exit 0 ;;\n"
        "esac\n",
    )
    install_app_stubs(bindir)
    return daemon, bindir, state


def _poll(daemon: Path, bindir: Path, state: Path) -> subprocess.CompletedProcess[str]:
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


def test_poll_skips_the_cycle_while_the_pause_holds(tmp_path: Path):
    daemon, bindir, state = _poll_setup(tmp_path)
    deadline = int(time.time()) + 1800
    _pause_file(state).write_text(f"{deadline}\n")
    r = _poll(daemon, bindir, state)
    # Exit 0: a quota pause is a deliberate skip, and run.sh logs non-zero as a
    # cycle failure.
    assert r.returncode == 0
    assert "session limit pause active" in r.stderr
    assert not (tmp_path / "gh.calls").exists()
    assert _pause_file(state).exists()


def test_poll_resumes_and_clears_the_file_once_the_pause_passes(tmp_path: Path):
    daemon, bindir, state = _poll_setup(tmp_path)
    _pause_file(state).write_text("1\n")
    r = _poll(daemon, bindir, state)
    assert r.returncode == 0
    assert "session limit pause active" not in r.stderr
    assert not _pause_file(state).exists()
    assert (tmp_path / "gh.calls").exists()
