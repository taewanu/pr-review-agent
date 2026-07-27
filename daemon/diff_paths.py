"""Parse a file path from a unified-diff `--- a/…` / `+++ b/…` header (#13).

The `diff --git a/OLD b/NEW` line is ambiguous for a path that contains the
substring ` b/`: git does not quote a plain space there, so `a/(.+) b/(.+)`
splits at the wrong ` b/`. The `---`/`+++` lines carry the path as the sole
field, tab-terminated when it holds a space and C-quoted in double quotes when
it holds a byte git escapes (non-ASCII, control, `"`, `\\`). That single-path
form has no split to get wrong, so every reader of a diff path goes through here.

Run as `python3 diff_paths.py <unified-diff-file>` to print one line per file,
named as it is after the change.
That entry point exists so shell callers have somewhere correct to go: an
earlier `diff_paths` helper in `lib.sh` matched the `diff --git` line and
dropped exactly the paths this module was written to keep (#306).
"""

from __future__ import annotations

import sys
from pathlib import Path

# git C-style escapes inside a quoted path. Octal `\ooo` bytes are handled
# separately; these are the named single-character escapes.
_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}


def _dequote(rest: str) -> str | None:
    """Decode a git C-quoted path. `rest` starts at the opening double-quote.

    Rebuilds the raw bytes (octal escapes are UTF-8 bytes, not code points) and
    decodes them, so a multibyte character escaped as `\\303\\256` becomes the one
    character it encodes. Returns None on an unterminated quote (malformed)."""
    out = bytearray()
    i = 1  # skip the opening quote
    while i < len(rest):
        c = rest[i]
        if c == '"':
            return out.decode("utf-8", "surrogateescape")
        if c == "\\" and i + 1 < len(rest):
            nxt = rest[i + 1]
            if nxt in "01234567":
                octal = rest[i + 1 : i + 4]
                # git emits exactly three octal digits for a byte (\000-\377).
                # Anything shorter or larger is malformed; degrade to None like
                # an unterminated quote rather than raising out of the parser.
                if len(octal) < 3 or any(d not in "01234567" for d in octal):
                    return None
                byte = int(octal, 8)
                if byte > 0xFF:
                    return None
                out.append(byte)
                i += 4
            else:
                out.append(_C_ESCAPES.get(nxt, ord(nxt)))
                i += 2
        else:
            out.extend(c.encode("utf-8"))
            i += 1
    return None


def parse_diff_path(line: str, marker: str) -> str | None:
    """The path from a `--- a/…` (marker `a/`) or `+++ b/…` (marker `b/`) header.

    Returns None for `/dev/null` (an added or deleted side has no path on that
    side) and for any line that is not that header, so a caller can try it on
    every line. Strips the `a/`/`b/` prefix git prepends to a real path."""
    prefix = "--- " if marker == "a/" else "+++ "
    if not line.startswith(prefix):
        return None
    rest = line[len(prefix) :]
    if rest.startswith('"'):
        path = _dequote(rest)
        if path is None:
            return None
    else:
        # A space-bearing path is terminated by a tab; split it off.
        path = rest.split("\t", 1)[0]
    if not path.startswith(marker):
        return None  # /dev/null, or a prefix git did not tag
    return path[len(marker) :]


def parse_rename_path(line: str) -> str | None:
    """The path from a `rename from …` / `rename to …` header, else None.

    A pure rename carries no `---`/`+++` pair at all, so these lines are the
    only record that the file moved. They hold the path as the sole field, in
    the same quoted-or-tab-terminated form, minus the `a/`/`b/` prefix.
    """
    for prefix in ("rename from ", "rename to "):
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix) :]
        if rest.startswith('"'):
            return _dequote(rest)
        return rest.split("\t", 1)[0]
    return None


def _file_blocks(text: str) -> list[list[str]]:
    """The diff split into one list of lines per `diff --git` header."""
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            blocks.append([])
        if blocks:
            blocks[-1].append(line)
    return blocks


def paths_in_diff(text: str) -> list[str]:
    """Every path the diff touches, both sides, deduped in first-seen order.

    For a rename this yields the old name and the new one, because a caller
    asking what the diff touched is asking about both: a file renamed from
    `.py` to `.md` removed code even though the surviving name says otherwise.
    Callers wanting one entry per file want `changed_files`.
    """
    seen: dict[str, None] = {}
    for line in text.splitlines():
        for path in (
            parse_diff_path(line, "a/"),
            parse_diff_path(line, "b/"),
            parse_rename_path(line),
        ):
            if path is not None:
                seen.setdefault(path, None)
    return list(seen)


def changed_files(text: str) -> list[str]:
    """One entry per file the diff touches, named as it is after the change.

    A rename is one file, listed under its new name; a deletion is one file,
    listed under the only name it had. This is what a file list a human reads
    wants, and it is what GitHub's own file count agrees with.
    """
    files: dict[str, None] = {}
    for block in _file_blocks(text):
        rename_to: str | None = None
        post: str | None = None
        pre: str | None = None
        for line in block:
            if line.startswith("rename to "):
                rename_to = parse_rename_path(line)
            elif post is None:
                post = parse_diff_path(line, "b/")
            if pre is None:
                pre = parse_diff_path(line, "a/")
        # Post-image name first, since that is what the file is called now.
        # A deletion has no post-image, so its pre-image name is the only one.
        chosen = rename_to or post or pre
        if chosen is not None:
            files.setdefault(chosen, None)
    return list(files)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: diff_paths.py <unified-diff-file>", file=sys.stderr)
        return 2
    try:
        text = Path(argv[0]).read_text(errors="replace")
    except OSError:
        return 0  # an unreadable diff prints nothing, matching the shell caller
    for path in changed_files(text):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
