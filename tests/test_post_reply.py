"""Round-trip + schema tests for daemon/post_reply.py (#36).

The reply poster used to be a bash `python heredoc | jq | while read -r | gh`
pipeline. #36 collapses it to one Python process so the body bytes survive end
to end. These tests pin that: a body carrying `\n`, `\t`, `\\`, a backticked
regex `` `\n[^\n]` `` and non-ASCII must reach `gh ... --input -` byte-for-byte.

`gh` is stubbed via a tmpdir on PATH (mirrors test_status_comment), but the stub
additionally records its stdin — that captured payload is the wire image whose
`body` we assert equals the input plus the addressed-sentinel footer.
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


def _raw(replies: list[dict]) -> str:
    """Wrap replies in the ```json fence the reply agent emits, with noise
    around it so the last-fence extraction is exercised."""
    return "agent preamble\n```json\n" + json.dumps({"replies": replies}) + "\n```\ntrailing prose"


def _run(
    raw: str, *, dry_run: bool = False, gh_exit: int = 0
) -> tuple[subprocess.CompletedProcess, str, str]:
    """Run post_reply.py with a stdin-recording `gh` stub. Returns
    (result, captured gh stdin, captured gh argv)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        raw_file = tmpd / "raw.txt"
        raw_file.write_text(raw)
        stdin_log = tmpd / "gh_stdin.txt"
        argv_log = tmpd / "gh_argv.txt"
        stub = tmpd / "gh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{argv_log}"\n'
            f'cat >> "{stdin_log}"\n'
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
        stdin = stdin_log.read_text() if stdin_log.exists() else ""
        argv = argv_log.read_text() if argv_log.exists() else ""
        return result, stdin, argv


def _reply(**over) -> dict:
    base = {
        "in_reply_to_id": "111",
        "addressed_comment_id": "222",
        "body": NASTY_BODY,
        "mode": "confirmed",
    }
    base.update(over)
    return base


def test_post_path_round_trips_body_byte_exact():
    result, stdin, argv = _run(_raw([_reply()]))
    assert result.returncode == 0, result.stderr
    # POSTed to the threaded-replies endpoint for the parent comment.
    assert "pulls/999/comments/111/replies" in argv
    sent = json.loads(stdin)
    expected = NASTY_BODY + "\n\n<!-- pr-review-agent:addressed:222 -->"
    assert sent["body"] == expected


def test_dry_run_emits_byte_exact_payload_without_calling_gh():
    result, _, argv = _run(_raw([_reply()]), dry_run=True)
    assert result.returncode == 0, result.stderr
    assert argv == "", "dry-run must not invoke gh"
    out = json.loads(result.stdout)
    expected = NASTY_BODY + "\n\n<!-- pr-review-agent:addressed:222 -->"
    assert out["posts"][0]["in_reply_to_id"] == "111"
    assert out["posts"][0]["payload"]["body"] == expected


def test_mode_defaults_to_confirmed():
    # No `mode` key — #37 default applies and the count line reflects it.
    reply = {"in_reply_to_id": "1", "addressed_comment_id": "2", "body": "x"}
    result, _, _ = _run(_raw([reply]), dry_run=True)
    assert result.returncode == 0, result.stderr
    assert "1 reply/replies ready (1 confirmed, 0 pushback)" in result.stderr


def test_empty_replies_exits_zero_without_gh():
    result, _, argv = _run(_raw([]))
    assert result.returncode == 0
    assert argv == "", "no replies means no POST"


def test_no_fence_is_no_fence_category():
    result, _, _ = _run("the agent said no JSON at all")
    assert result.returncode == 1
    assert "category=no-fence" in result.stderr


def test_invalid_json_is_parse_error_category():
    result, _, _ = _run("```json\n{not: valid, json,}\n```")
    assert result.returncode == 1
    assert "category=parse-error" in result.stderr


def test_missing_required_key_is_schema_invalid():
    reply = {"in_reply_to_id": "1", "body": "x"}  # no addressed_comment_id
    result, _, _ = _run(_raw([reply]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


def test_bad_mode_is_schema_invalid():
    result, _, _ = _run(_raw([_reply(mode="bogus")]))
    assert result.returncode == 1
    assert "category=schema-invalid" in result.stderr


def test_post_failure_is_best_effort_exit_zero():
    # A failed POST leaves no sentinel, so the next polling cycle retries — the
    # script stays 0 rather than aborting the tick.
    result, _, _ = _run(_raw([_reply()]), gh_exit=1)
    assert result.returncode == 0
    assert "reply POST failed" in result.stderr
