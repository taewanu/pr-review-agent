"""Guard: every gh invocation in daemon/ runs under run_with_app_token (ADR 0036).

Under App identity a bare `gh` falls back to the operator's stored login and
posts under the human identity the swap replaces, and nothing else in the suite
notices: the review still lands, just as the wrong actor. There is no
compile-time signal for a missed wrap, so this scan is it. Every gh command must
have run_with_app_token textually before it on the same logical line (the wrapper
puts gh on a continuation line after the run_with_app_token call).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON = REPO_ROOT / "daemon"

# A gh command invocation: `gh <subcommand>`. Excludes the `gh)` case pattern in
# run_with_app_token's allowlist (no space) and prose mentions handled below.
_GH_CMD = re.compile(r"\bgh\s+\S")


def _logical_lines(text: str) -> list[str]:
    """Join backslash-continuations so a wrapped call (run_with_app_token on one
    line, gh on the next) reads as one line the check can reason about."""
    out: list[str] = []
    buf = ""
    for line in text.split("\n"):
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
        else:
            out.append(buf + line)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _in_string_or_comment(prefix: str) -> bool:
    """True when the text before a match sits inside a double-quoted string or
    past an unquoted `#` (a comment): a gh in log prose, not a command."""
    in_quote = False
    for ch in prefix:
        if ch == '"':
            in_quote = not in_quote
        elif ch == "#" and not in_quote:
            return True  # comment start
    return in_quote


def _is_command_position(prefix: str) -> bool:
    """True when a gh at this point starts a simple command, rather than sitting
    as a bareword argument (e.g. `for cmd in gh jq …`). A command follows
    line-start, a pipe/subshell/negation/separator, or then/do/else."""
    before = prefix.rstrip()
    if before == "":
        return True
    if before[-1] in "|(!;&":
        return True
    return before.split()[-1] in ("then", "do", "else")


def _bare_gh(path: Path) -> list[str]:
    violations = []
    for logical in _logical_lines(path.read_text()):
        for match in _GH_CMD.finditer(logical):
            prefix = logical[: match.start()]
            if _in_string_or_comment(prefix):
                continue
            if "run_with_app_token" in prefix:
                continue  # wrapped
            if _is_command_position(prefix):
                violations.append(f"{path.name}: {logical.strip()}")
    return violations


def test_every_gh_call_in_daemon_is_wrapped():
    violations: list[str] = []
    for sh in sorted(DAEMON.glob("*.sh")):
        violations += _bare_gh(sh)
    assert violations == [], "unwrapped gh calls (would post as the operator):\n" + "\n".join(
        violations
    )


def test_the_guard_catches_a_planted_bare_call(tmp_path):
    # The guard is only worth having if it fails on a real bare call; prove it
    # does rather than trust an empty result on the real tree.
    planted = tmp_path / "planted.sh"
    planted.write_text('#!/usr/bin/env bash\ngh api "repos/$1/pulls" --jq ".[]"\n')
    assert _bare_gh(planted), "the guard missed a bare gh api call"

    wrapped = tmp_path / "wrapped.sh"
    wrapped.write_text(
        '#!/usr/bin/env bash\nrun_with_app_token "$A" "$I" \\\n  gh api "repos/$1/pulls"\n'
    )
    assert not _bare_gh(wrapped), "the guard flagged a correctly wrapped call"
