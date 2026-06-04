"""Round-trip + schema tests for daemon/post_reply.py (#36, #55).

The reply poster used to be a bash `python heredoc | jq | while read -r | gh`
pipeline. #36 collapses it to one Python process so the body bytes survive end
to end. #55 adds a per-thread pickup reaction. These tests pin both: a body
carrying `\n`, `\t`, `\\`, a backticked regex `` `\n[^\n]` `` and non-ASCII
must reach `gh ... --input -` byte-for-byte, and each bucket must POST its
reaction.

`gh` is stubbed via a tmpdir on PATH (mirrors test_status_comment). The stub
records each call's argv and stdin on its own line so a fix_claim's two POSTs
(reply + reaction) stay separable.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POST_REPLY = REPO_ROOT / "daemon" / "post_reply.py"

# The chars the old bash pipeline could mangle: newline, tab, backslash, a
# backticked regex with a literal `\n`, and non-ASCII.
NASTY_BODY = (
    "first line\n"
    "second\twith a tab\n"
    "a literal backslash \\ here\n"
    "regex `\\n[^\\n]` in backticks\n"
    "unicode 안녕 café"
)

EXPECTED_BODY = NASTY_BODY + "\n\n<!-- pr-review-agent:addressed:222 -->"


def _raw(replies: list[dict]) -> str:
    """Wrap replies in the ```json fence the reply agent emits, with noise
    around it so the last-fence extraction is exercised."""
    return "agent preamble\n```json\n" + json.dumps({"replies": replies}) + "\n```\ntrailing prose"


def _run(
    raw: str, *, dry_run: bool = False, gh_exit: int = 0
) -> tuple[subprocess.CompletedProcess, list[tuple[str, str]]]:
    """Run post_reply.py with a per-call-recording `gh` stub. Returns
    (result, calls) where calls is a list of (argv, stdin) tuples in order."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        raw_file = tmpd / "raw.txt"
        raw_file.write_text(raw)
        stdin_log = tmpd / "gh_stdin.txt"
        argv_log = tmpd / "gh_argv.txt"
        stub = tmpd / "gh"
        # One line per invocation in each log, so argv[i] pairs with stdin[i].
        # stdin payloads are single-line JSON (json.dumps escapes newlines), so
        # a trailing \n is a safe record separator.
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{argv_log}"\n'
            f'cat >> "{stdin_log}"\n'
            f'printf "\\n" >> "{stdin_log}"\n'
            f"exit {gh_exit}\n"
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = os.environ.copy()
        env["PATH"] = f"{tmpd}:{env['PATH']}"
        args = [
            "python3",
            str(POST_REPLY),
            "--owner",
            "example",
            "--repo",
            "example",
            "--number",
            "999",
            "--raw",
            str(raw_file),
        ]
        if dry_run:
            args.append("--dry-run")
        result = subprocess.run(args, capture_output=True, text=True, env=env)
        argv_lines = argv_log.read_text().splitlines() if argv_log.exists() else []
        stdin_lines = stdin_log.read_text().splitlines() if stdin_log.exists() else []
        calls = list(zip(argv_lines, stdin_lines, strict=True))
        return result, calls


def _reply(**over) -> dict:
    base = {
        "in_reply_to_id": "111",
        "addressed_comment_id": "222",
        "bucket": "fix_claim",
        "mode": "confirmed",
        "body": NASTY_BODY,
    }
    base.update(over)
    return base


def _find(calls: list[tuple[str, str]], needle: str) -> tuple[str, str] | None:
    return next((c for c in calls if needle in c[0]), None)


def test_fix_claim_round_trips_body_and_posts_eyes_reaction():
    result, calls = _run(_raw([_reply()]))
    assert result.returncode == 0, result.stderr

    reply_call = _find(calls, "pulls/999/comments/111/replies")
    assert reply_call is not None, "fix_claim must POST a threaded reply"
    assert json.loads(reply_call[1])["body"] == EXPECTED_BODY

    react_call = _find(calls, "pulls/comments/222/reactions")
    assert react_call is not None, "fix_claim must react on the operator comment"
    assert json.loads(react_call[1]) == {"content": "eyes"}


def test_acknowledgment_posts_plus_one_reaction_only():
    reply = {"in_reply_to_id": "1", "addressed_comment_id": "222", "bucket": "acknowledgment"}
    result, calls = _run(_raw([reply]))
    assert result.returncode == 0, result.stderr
    assert _find(calls, "/replies") is None, "acknowledgment posts no text reply"
    react_call = _find(calls, "pulls/comments/222/reactions")
    assert react_call is not None
    assert json.loads(react_call[1]) == {"content": "+1"}


def test_question_posts_eyes_reaction_only():
    reply = {"in_reply_to_id": "1", "addressed_comment_id": "222", "bucket": "question"}
    result, calls = _run(_raw([reply]))
    assert result.returncode == 0, result.stderr
    assert _find(calls, "/replies") is None, "question posts no text reply"
    react_call = _find(calls, "pulls/comments/222/reactions")
    assert react_call is not None
    assert json.loads(react_call[1]) == {"content": "eyes"}


def test_dry_run_emits_plan_without_calling_gh():
    result, calls = _run(_raw([_reply()]), dry_run=True)
    assert result.returncode == 0, result.stderr
    assert calls == [], "dry-run must not invoke gh"
    plan = json.loads(result.stdout)["plan"]
    assert plan[0]["bucket"] == "fix_claim"
    assert plan[0]["reaction"] == "eyes"
    assert plan[0]["in_reply_to_id"] == "111"
    assert plan[0]["reply_payload"]["body"] == EXPECTED_BODY


def test_dry_run_non_claim_has_reaction_no_reply_payload():
    reply = {"in_reply_to_id": "1", "addressed_comment_id": "222", "bucket": "acknowledgment"}
    result, _ = _run(_raw([reply]), dry_run=True)
    assert result.returncode == 0, result.stderr
    entry = json.loads(result.stdout)["plan"][0]
    assert entry["reaction"] == "+1"
    assert "reply_payload" not in entry
    assert "in_reply_to_id" not in entry


def test_mode_defaults_to_confirmed_for_fix_claim():
    # No `mode` key — #37 default applies and the count line reflects the bucket.
    reply = {"in_reply_to_id": "1", "addressed_comment_id": "2", "bucket": "fix_claim", "body": "x"}
    result, _ = _run(_raw([reply]), dry_run=True)
    assert result.returncode == 0, result.stderr
    assert "1 thread(s): 1 fix-claim, 0 question, 0 acknowledgment" in result.stderr


def test_empty_replies_exits_zero_without_gh():
    result, calls = _run(_raw([]))
    assert result.returncode == 0
    assert calls == [], "no threads means no POST"


def test_reaction_post_failure_is_best_effort_exit_zero():
    # A failed reaction POST must not abort the polling cycle (best-effort,
    # matching the text-reply path).
    result, _ = _run(_raw([_reply()]), gh_exit=1)
    assert result.returncode == 0
    assert "reply POST failed" in result.stderr
    assert "reaction POST failed" in result.stderr


def test_no_fence_is_no_fence_category():
    result, _ = _run("the agent said no JSON at all")
    assert result.returncode == 1
    assert "category=no-fence" in result.stderr


def test_invalid_json_is_parse_error_category():
    result, _ = _run("```json\n{not: valid, json,}\n```")
    assert result.returncode == 1
    assert "category=parse-error" in result.stderr


def test_missing_required_key_is_schema_invalid():
    reply = {"in_reply_to_id": "1", "bucket": "acknowledgment"}  # no addressed_comment_id
    result, _ = _run(_raw([reply]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


def test_bad_bucket_is_schema_invalid():
    result, _ = _run(_raw([_reply(bucket="bogus")]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


def test_fix_claim_missing_body_is_schema_invalid():
    reply = {"in_reply_to_id": "1", "addressed_comment_id": "2", "bucket": "fix_claim"}
    result, _ = _run(_raw([reply]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


def test_bad_mode_is_schema_invalid():
    result, _ = _run(_raw([_reply(mode="bogus")]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr
