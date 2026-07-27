"""Parse a file path from a unified-diff `--- a/…` / `+++ b/…` header (#13).

The `diff --git a/OLD b/NEW` line is ambiguous for a path that contains the
substring ` b/`: git does not quote a plain space there, so `a/(.+) b/(.+)`
splits at the wrong ` b/`. The `---`/`+++` lines carry the path as the sole
field, tab-terminated when it holds a space and C-quoted in double quotes when
it holds a byte git escapes (non-ASCII, control, `"`, `\\`). That single-path
form has no split to get wrong, so every reader of a diff path goes through here.

Run as `python3 diff_paths.py <unified-diff-file>` to print one path per line.
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


def paths_in_diff(text: str) -> list[str]:
    """Every path the diff touches, deduped, in first-seen order.

    Reads both header sides, so a file whose `+++` is `/dev/null` still yields
    the path from its `---`, and an addition still yields the path from its
    `+++`.
    """
    seen: dict[str, None] = {}
    for line in text.splitlines():
        for marker in ("a/", "b/"):
            path = parse_diff_path(line, marker)
            if path is not None:
                seen.setdefault(path, None)
    return list(seen)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: diff_paths.py <unified-diff-file>", file=sys.stderr)
        return 2
    try:
        text = Path(argv[0]).read_text(errors="replace")
    except OSError:
        return 0  # an unreadable diff prints nothing, matching the shell caller
    for path in paths_in_diff(text):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
