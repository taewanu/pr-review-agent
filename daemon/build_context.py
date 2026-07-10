#!/usr/bin/env python3
"""Build a shared review context pack, once, before the lens fan-out (ADR 0029).

The five review lenses (ADR 0023) each investigate the same repository to verify
their candidates (ADR 0022's verify step). That investigation is near-identical
across lenses, so this stage runs it once, deterministically, and hands every
lens the result. No model call: the pack is git plus grep, so it adds no frontier
or cheap-model cost. It is additive and lossless (a lens keeps its own tools), so
it cannot regress recall, only the cost of five-fold re-investigation.

The pack holds the retrieval a lens would otherwise grep for: the changed
symbols' references across the repo (a grep approximation of CodeRabbit's AST call
graph), the related tests, and a short project-context header. The changed files'
own content is not inlined: they already sit in the scratch working tree the lens
can Read directly."""

import argparse
import re
import subprocess
import sys
from pathlib import Path

# daemon/ is not a package and this runs by path, so add its own dir before
# importing the sibling diff walker (same idiom merge_findings uses).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import anchor_findings  # noqa: E402

# Per symbol, cap how many reference hits reach the pack: a common name (e.g.
# `main`, `run`) would otherwise flood the pack with unrelated matches, the
# over-inclusion cost ADR 0029 names. The lens's retained Grep covers anything
# truncated here.
_MAX_REFS_PER_SYMBOL = 20
# Lines of surrounding context to show around each reference hit, so a lens reads
# the call site's shape without opening the file.
_REF_CONTEXT_LINES = 2


def changed_added_lines(diff_text: str) -> dict[str, list[str]]:
    """Map each changed new-side path to the text of its added lines.

    Reuses anchor_findings' diff walker (the single home for the new-side line
    state machine). Only added (`+`) lines, not context: a symbol a lens cares
    about is one this diff introduces or edits, not one that merely sits nearby.
    The leading `+` marker is stripped; the code text is what symbol extraction
    reads."""
    by_path: dict[str, list[str]] = {}
    current_path: str | None = None
    for dl in anchor_findings._iter_diff_lines(diff_text):
        if dl.new_path is not None:
            # _iter_diff_lines sets new_path only on a `diff --git` header, so
            # this tracks the file whose hunks follow, the same local-variable
            # idiom the walker's other consumer uses (anchor_findings.Diff.parse).
            current_path = dl.new_path
            by_path.setdefault(current_path, [])
        elif dl.new_lineno is not None and dl.raw.startswith("+") and current_path is not None:
            by_path[current_path].append(dl.raw[1:])
    return by_path


# Definitions worth a reference map: Python def/class and bash `name()` and
# `function name`. Anchored to the line start (after indentation) so a `def` in
# a comment or a call mid-line isn't matched. Assignments are deliberately
# excluded: a changed constant floods the grep with unrelated same-named matches
# for little caller-tracing value, the precision bias ADR 0029's guidance sets.
_DEF_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)"),  # python def
    re.compile(r"^\s*class\s+(?P<name>[A-Za-z_]\w*)"),  # python class
    re.compile(r"^\s*(?P<name>[A-Za-z_]\w*)\s*\(\s*\)\s*\{"),  # bash name() {
    re.compile(r"^\s*function\s+(?P<name>[A-Za-z_]\w*)"),  # bash function name
)
# Names too generic to trace usefully: a grep for one floods the pack with
# unrelated hits. Dropped even when a diff genuinely (re)defines them.
_TOO_COMMON = frozenset({"main", "run", "setup", "init", "test", "handler"})
_MIN_SYMBOL_LEN = 3


def extract_symbols(added_lines_by_path: dict[str, list[str]]) -> set[str]:
    """Return the set of symbol names the diff introduces or edits, to grep for.

    Precision-biased per ADR 0029: only names a diff line *defines* (a function
    or class definition), not every identifier it mentions, so the reference grep
    traces real callers rather than incidental matches. Under-inclusion (indirect
    calls, generic names dropped below) is covered by each lens's retained Grep;
    over-inclusion costs pack bytes the _MAX_REFS_PER_SYMBOL cap bounds."""
    symbols: set[str] = set()
    for lines in added_lines_by_path.values():
        for line in lines:
            for pat in _DEF_PATTERNS:
                m = pat.match(line)
                if m:
                    name = m.group("name")
                    if len(name) >= _MIN_SYMBOL_LEN and name.lower() not in _TOO_COMMON:
                        symbols.add(name)
    return symbols


def _tracked_files(repo_root: Path) -> list[Path]:
    """Every git-tracked file under repo_root, for the reference grep. Restricted
    to `git ls-files`, not a tree walk, on purpose: an untracked file (a build
    artifact like `__pycache__/*.pyc`, or a secret like an untracked `.env`) must
    never land in a pack that ships to a model. A non-git root or a missing git
    binary returns nothing, degrading the pack to no references rather than
    failing (the pack is best-effort, ADR 0029)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [repo_root / rel for rel in out.split("\0") if rel]


def find_references(
    symbols: set[str], repo_root: Path, changed_paths: set[str]
) -> dict[str, list[str]]:
    """For each symbol, grep the tracked files for word-boundary references,
    excluding the changed files themselves (already in the lens's working tree).

    Returns symbol -> list of `path:line: text` rows (with a little surrounding
    context), capped per symbol. This is the caller map: where the changed code
    is used from, so a lens can build a trigger scenario without re-deriving it. A
    name grep gives references, not directed callees, so this is caller-side."""
    refs: dict[str, list[str]] = {}
    patterns = {s: re.compile(rf"\b{re.escape(s)}\b") for s in symbols}
    for f in _tracked_files(repo_root):
        rel = str(f.relative_to(repo_root))
        if rel in changed_paths:
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            for sym, pat in patterns.items():
                if len(refs.get(sym, [])) >= _MAX_REFS_PER_SYMBOL:
                    continue
                if pat.search(line):
                    lo = max(0, i - _REF_CONTEXT_LINES)
                    hi = min(len(lines), i + _REF_CONTEXT_LINES + 1)
                    snippet = "\n".join(f"    {lines[j]}" for j in range(lo, hi))
                    refs.setdefault(sym, []).append(f"{rel}:{i + 1}:\n{snippet}")
    return refs


def find_related_tests(changed_paths: set[str], symbols: set[str], repo_root: Path) -> list[str]:
    """Test files that reference a changed file's basename or a changed symbol.

    A lens's test-coverage check (review-agent-tests) otherwise greps for these;
    surfacing them once means the lens sees which tests already exercise the
    changed code without re-deriving the mapping."""
    basenames = {Path(p).stem for p in changed_paths}
    needles = basenames | symbols
    hits: list[str] = []
    for f in _tracked_files(repo_root):
        rel = str(f.relative_to(repo_root))
        if "test" not in rel.lower():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(n and n in text for n in needles):
            hits.append(rel)
    return hits


def project_header(repo_root: Path) -> str:
    """A short project-context header: the ADR index (titles only). CLAUDE.md is
    already loaded into every agent's context by the harness, so it is not
    duplicated here; the ADR titles are what a lens's ADR-violation check would
    otherwise list the tree to find."""
    lines: list[str] = []
    adr_dir = repo_root / "docs" / "adr"
    if adr_dir.is_dir():
        for adr in sorted(adr_dir.glob("*.md")):
            first = adr.read_text(encoding="utf-8", errors="replace").splitlines()
            title = next((ln.lstrip("# ").strip() for ln in first if ln.startswith("#")), adr.stem)
            lines.append(f"- {title}")
    return "\n".join(lines)


def build_pack(diff_text: str, repo_root: Path) -> str:
    """Assemble the full context pack text from the diff and the repo at HEAD."""
    added = changed_added_lines(diff_text)
    changed_paths = set(added)
    symbols = extract_symbols(added) or set()
    refs = find_references(symbols, repo_root, changed_paths)
    tests = find_related_tests(changed_paths, symbols, repo_root)

    out: list[str] = ["# Review context pack (ADR 0029)", ""]
    out.append("## Architectural decisions (titles)")
    out.append(project_header(repo_root) or "(none)")
    out.append("")
    out.append("## Changed symbols")
    out.append(", ".join(sorted(symbols)) if symbols else "(none extracted)")
    out.append("")
    out.append("## References to changed symbols (grep, capped)")
    if refs:
        for sym in sorted(refs):
            out.append(f"### {sym}")
            out.extend(refs[sym])
            out.append("")
    else:
        out.append("(no references found outside the changed files)")
        out.append("")
    out.append("## Related tests")
    out.append("\n".join(f"- {t}" for t in tests) if tests else "(none found)")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the shared review context pack (ADR 0029).")
    ap.add_argument("--diff", required=True, help="path to the gh pr diff output")
    ap.add_argument("--repo-root", default=".", help="scratch clone root, at PR HEAD")
    args = ap.parse_args(argv)

    diff_text = Path(args.diff).read_text(encoding="utf-8", errors="replace")
    sys.stdout.write(build_pack(diff_text, Path(args.repo_root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
