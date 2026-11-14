#!/usr/bin/env python3
"""The codex side of lib.sh's run_agent: builds the `codex exec` argv, and renders
its `--json` events as the same activity lines and closing line stream_format.py
prints for claude.

`argv` prints the argv NUL-separated, so a path or prompt holding a newline
survives the trip into a bash array. `format` follows the events file codex
writes until codex's process is gone; codex itself writes the final message to
the `-o` file, so the formatter only narrates and records the token count.
`check` resolves codex on PATH the way `argv` does, for a startup check, checks
that it accepts that argv, and prints which binary it chose when a shim came
first.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from stream_format import (
    clone_roots,
    closing_line,
    describe_tool,
    emit,
    prose,
    text_columns,
)

PROFILE = "pr-review-agent"
# What codex exec prints, after validating its arguments, when stdin is empty.
NO_PROMPT = "No prompt provided via stdin."
# The shared temp roots on Linux and their real paths on macOS, where /tmp and
# /var/tmp are symlinks into /private.
TEMP_ROOTS = ("/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp")
# Events whose message is printed whole, since nothing else keeps it.
ERROR_EVENTS = ("turn.failed", "error")


def _toml_string(text: str) -> str:
    """A TOML basic string. JSON's escaping is TOML's, except that TOML also forbids
    a raw DEL, and an astral character escaped as a JSON surrogate pair is not a
    valid TOML escape, so non-ASCII stays literal."""
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def real_codex(codex_on_path: str | None) -> str:
    """The real path of the codex binary on PATH, or SystemExit saying what to fix."""
    if codex_on_path is None:
        raise SystemExit("codex is not on PATH")
    codex_bin = os.path.realpath(codex_on_path)
    if not os.path.basename(codex_bin).startswith("codex"):
        raise SystemExit(
            f"codex on PATH ({codex_on_path}) resolves to {codex_bin}, which is not a codex"
            " binary (a version-manager shim?); put the directory holding the real codex"
            " binary on PATH ahead of the shim"
        )
    return codex_bin


def find_codex(search_path: str) -> tuple[str, str | None]:
    """The real path of the first codex on `search_path` that is not a
    version-manager shim, and the first shim it skipped (None when it skipped
    none), or SystemExit saying what to fix. A relative entry is skipped because
    the working directory is the per-PR clone, whose files the PR author controls."""
    shims = []
    for entry in search_path.split(os.pathsep):
        candidate = os.path.join(entry, "codex")
        if not (
            os.path.isabs(entry) and os.path.isfile(candidate) and os.access(candidate, os.X_OK)
        ):
            continue
        try:
            return real_codex(candidate), shims[0] if shims else None
        except SystemExit:
            shims.append(candidate)
    # Every codex found is a shim, so real_codex raises the shim message.
    return real_codex(shims[0] if shims else None), None


def build_argv(
    codex_on_path: str, workdir: str, raw_out: str, model: str, effort: str, system: str | None
) -> list[str]:
    codex_bin = real_codex(codex_on_path)
    # Codex re-executes its own binary inside the sandbox, so the profile must let
    # it read the install. It runs by its real path because a read grant on the
    # binary's directory does not cover the symlinks between the PATH entry and
    # the binary, and the grant is the install root, the parent of the binary's
    # directory, because some installs keep the native binary beside a launcher.
    install_root = os.path.dirname(os.path.dirname(codex_bin))
    argv = [
        codex_bin,
        "exec",
        # An unknown -c key is otherwise dropped in silence, and the keys below
        # are what bound the agent.
        "--strict-config",
        # The operator's own config, execpolicy rules, and session history stay
        # out of the review; --ignore-rules also drops rules files the clone
        # carries, which the PR author controls.
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        workdir,
        "-m",
        model,
        *sandbox_config(install_root),
        # Never --sandbox: passing it makes codex ignore default_permissions.
        "-c",
        f'default_permissions="{PROFILE}"',
        "-c",
        f"model_reasoning_effort={_toml_string(effort)}",
    ]
    if system:
        argv += ["-c", f"developer_instructions={_toml_string(Path(system).read_text())}"]
    argv += [
        # Codex loads AGENTS.md and AGENTS.override.md from the clone, which
        # would let the PR author instruct the reviewer; a zero byte budget loads
        # none of either.
        "-c",
        "project_doc_max_bytes=0",
        # Skills (the clone's .agents/skills and .codex/skills among them) and
        # plugins load even under --ignore-user-config.
        "-c",
        "skills.include_instructions=false",
        "-c",
        "skills.bundled.enabled=false",
        "-c",
        "plugins._default.enabled=false",
        "--disable",
        "apps",
        "--disable",
        "plugins",
        "--disable",
        "hooks",
        "--disable",
        "image_generation",
        # A spawned subagent is a second model session outside the shared slot
        # pool, which caps concurrent agent calls.
        "--disable",
        "multi_agent",
        "--disable",
        "multi_agent_v2",
        "-c",
        'web_search="disabled"',
        # Nobody is there to approve, so a command the profile denies fails
        # rather than waiting.
        "-c",
        'approval_policy="never"',
        "-c",
        "allow_login_shell=false",
        "--json",
        "-o",
        raw_out,
        "-",
    ]
    return argv


def check_accepts_argv(codex_bin: str) -> None:
    """Runs the argv every agent call sends, minus the prompt, or SystemExit with
    what codex said. Codex parses every flag and, under --strict-config, every
    config key before it reads the prompt, and stops at the missing prompt; with
    no credentials it could not reach the model even if it went on. Every agent
    call carries a role definition, so the check carries one too."""
    with tempfile.TemporaryDirectory() as work:
        Path(system := os.path.join(work, "system.md")).write_text("startup check\n")
        argv = build_argv(
            codex_bin, work, os.path.join(work, "raw"), "startup-check", "high", system
        )
        env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
        os.mkdir(home := os.path.join(work, "home"))
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                cwd=work,
                env={**env, "CODEX_HOME": home},
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            raise SystemExit(f"{codex_bin} did not answer the startup check within 60s") from None
    output = proc.stderr + proc.stdout
    if NO_PROMPT not in output:
        said = next((line for line in output.splitlines() if line.strip()), "no output")
        raise SystemExit(f"{codex_bin} rejects what the daemon sends ({said}); run 'codex update'")


def sandbox_config(install_root: str) -> list[str]:
    """The `-c` pairs that bound what the agent's shell commands can reach."""
    # One inline table: codex rejects the profile as dotted keys. Read is limited
    # to the platform minimum, the codex install, and the clone; nothing is
    # writable and the network is off, because the clone is the PR author's.
    # On macOS, :minimal grants read and write on the system temp roots, and only
    # a glob deny overrides that grant (`X*` hides the root's listing, `X/**` its
    # contents). Linux expands a glob deny into the matching files by walking the
    # disk at sandbox start and aborts on a directory it cannot list, such as
    # systemd's root-only ones in /tmp, so there a path deny masks each root.
    if sys.platform == "darwin":
        temp_denies = "".join(f'"{root}*"="deny", "{root}/**"="deny", ' for root in TEMP_ROOTS)
    elif sys.platform == "linux":
        temp_denies = "".join(f'"{root}"="deny", ' for root in TEMP_ROOTS)
    else:
        # The path deny is safe only because Linux :minimal never mounts the temp roots.
        raise SystemExit(f"the codex sandbox is untested on {sys.platform}; use macOS or Linux")
    profile = (
        f'permissions.{PROFILE}={{filesystem={{":minimal"="read", {temp_denies}'
        f'{_toml_string(install_root)}="read", ":workspace_roots"={{"."="read"}}}}, '
        f"network={{enabled=false}}}}"
    )
    return [
        "-c",
        profile,
        # Commands otherwise inherit the daemon's environment, credentials and
        # agent sockets included. git reads ~/.gitconfig and its default ignore and
        # attributes files, all denied by the profile, so each is pointed at nothing.
        "-c",
        'shell_environment_policy.inherit="core"',
        "-c",
        "shell_environment_policy.set={"
        'GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_COUNT="2", '
        'GIT_CONFIG_KEY_0="core.excludesFile", GIT_CONFIG_VALUE_0="/dev/null", '
        'GIT_CONFIG_KEY_1="core.attributesFile", GIT_CONFIG_VALUE_1="/dev/null"}',
    ]


def render_event(event: dict, roots: tuple[str, ...] = (), width: int | None = None) -> str | None:
    """Map one `codex exec --json` event to a progress line, or None to skip it. A
    completed turn renders nothing here; format_stream closes the call once."""
    etype = event.get("type")
    if etype == "item.completed":
        item = event.get("item") or {}
        if item.get("type") == "command_execution":
            command = str(item.get("command", ""))
            return f"→ {describe_tool('Shell', command, roots, width=width and width - 2)}"
        if item.get("type") == "agent_message":
            return prose(str(item.get("text", "")), roots, width)
    elif etype in ERROR_EVENTS:
        error = event.get("error") if etype == "turn.failed" else event
        return f"{etype}: {' '.join(str((error or {}).get('message', '')).split())}"
    return None


def usage_tokens(event: dict) -> int:
    """Input plus output tokens; cached input is a subset of input, not an addend."""
    usage = event.get("usage") or {}
    return sum(
        int(v) for k in ("input_tokens", "output_tokens") if isinstance(v := usage.get(k), int)
    )


def follow(events: Path, pid: int, poll: float = 0.2) -> Iterator[str]:
    """Yield the lines appended to `events` until process `pid` is gone. One read
    after it is gone drains what it wrote; a process that escaped the kill and
    still writes there cannot keep this going."""
    pending = ""
    with events.open(errors="replace") as f:
        while True:
            alive = _alive(pid)
            chunk = f.read()
            *lines, pending = (pending + chunk).split("\n")
            yield from lines
            if not alive:
                break
            if not chunk:
                time.sleep(poll)
    if pending:
        yield pending


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def format_stream(lines, prefix: str, tokens_out: Path, clock=time.monotonic) -> int:
    """Exits 1 on turn.failed so a failure surfaces even if codex exits 0; on a
    timeout run_agent returns codex's own status."""
    start = clock()
    roots = clone_roots()
    failed = False
    tokens = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed":
            tokens = (tokens or 0) + usage_tokens(event)
        failed = failed or event.get("type") == "turn.failed"
        width = text_columns(prefix)
        rendered = render_event(event, roots, width)
        if rendered is not None:
            emit(rendered, prefix, None if event.get("type") in ERROR_EVENTS else width)
    # Written only on a completed turn, so a timeout leaves no sidecar and no
    # closing line, as on claude. Codex reports no cost, so the line names none.
    if tokens is not None:
        tokens_out.write_text(str(tokens))
        emit(closing_line(None, clock() - start, None, tokens), prefix, text_columns(prefix))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    argv_p = sub.add_parser("argv")
    argv_p.add_argument("--workdir", required=True)
    argv_p.add_argument("--raw-out", required=True)
    argv_p.add_argument("--model", required=True)
    argv_p.add_argument("--effort", required=True)
    argv_p.add_argument("--system")
    fmt_p = sub.add_parser("format")
    fmt_p.add_argument("--events", type=Path, required=True)
    fmt_p.add_argument("--while-pid", type=int, required=True)
    fmt_p.add_argument("--prefix", default="[pr-review-agent] ")
    fmt_p.add_argument("--tokens-out", type=Path, required=True)
    sub.add_parser("check")
    args = parser.parse_args()
    if args.cmd == "format":
        return format_stream(follow(args.events, args.while_pid), args.prefix, args.tokens_out)
    codex, skipped = find_codex(os.environ.get("PATH", os.defpath))
    if args.cmd == "check":
        check_accepts_argv(codex)
        if skipped:
            print(f"codex: using {codex}; {skipped} comes first on PATH but is a shim")
        return 0
    argv = build_argv(codex, args.workdir, args.raw_out, args.model, args.effort, args.system)
    sys.stdout.write("".join(f"{a}\0" for a in argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
