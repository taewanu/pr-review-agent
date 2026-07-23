#!/usr/bin/env python3
"""Read .env from the checkout root, validate, emit unified JSON.

Output goes to stdout (consumed by poll.sh via jq). Errors emit a parseable
`category=<slug>` first stderr line per ADR 0005 so the daemon can route them
through log_failure.

Usage:
    python3 daemon/load_config.py                # infer checkout root from script location
    python3 daemon/load_config.py /path/to/root  # override (for tests)
"""

import re
import sys
from pathlib import Path

from pydantic import BaseModel, ValidationError, field_validator

REPO_RE = re.compile(r"^[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*$")
ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

# A generous guard against a typo'd MAX_PARALLEL, not a tuning knob: well above
# the advised 2-3 so it never blocks legit use, low enough to catch 30/50/100.
MAX_PARALLEL_CEILING = 16


class ConfigError(Exception):
    """Categorised config-load failure for ADR 0005 routing."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


def parse_env(text: str) -> dict[str, str]:
    """Parse a minimal .env subset: KEY=value, KEY="value", KEY='value', # comments, blanks.

    No escape sequences and no variable expansion — kept narrow on purpose so the
    bash `source .env` and this parser stay aligned on the common cases.
    """
    env: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = ENV_LINE_RE.match(raw)
        if not m:
            raise ConfigError(
                "config-parse-error", f".env line {lineno}: expected KEY=value, got: {raw!r}"
            )
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        env[key] = value
    return env


class DaemonConfig(BaseModel):
    """Every field here is consumed by the daemon (#199): a key the pipeline
    does not read does not belong in the schema, because a parsed-but-unread
    key is a silent no-op the operator can't distinguish from a working one.
    Reviewer-side tunables (CONFIDENCE_THRESHOLD, MAX_FINDINGS) live in .env
    and are resolved by review-pr.sh, not here."""

    repos: list[str]
    github_app_id: str
    poll_interval_seconds: int = 300
    max_parallel: int = 1
    opt_out_label: str = "no-ai-review"

    @field_validator("repos")
    @classmethod
    def _check_repos(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("REPOS is empty — at least one owner/repo required")
        for r in v:
            if not REPO_RE.match(r):
                raise ValueError(f"invalid REPOS entry {r!r} — expected owner/repo")
        return v

    @field_validator("github_app_id")
    @classmethod
    def _check_app_id(cls, v: str) -> str:
        # Numeric because GitHub App ids are integers; a non-digit value is a
        # typo the boot check catches instead of a failed JWT mint later.
        if not v.strip().isdigit():
            raise ValueError(f"GITHUB_APP_ID must be a numeric App id (got {v!r})")
        return v.strip()

    @field_validator("poll_interval_seconds")
    @classmethod
    def _check_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"POLL_INTERVAL_SECONDS must be >= 1 (got {v})")
        return v

    @field_validator("max_parallel")
    @classmethod
    def _check_max_parallel(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"MAX_PARALLEL must be >= 1 (got {v})")
        if v > MAX_PARALLEL_CEILING:
            raise ValueError(
                f"MAX_PARALLEL must be <= {MAX_PARALLEL_CEILING} (got {v}); "
                "each unit is a concurrent claude review"
            )
        return v


def _parse_int(s: str, *, key: str, default: int) -> int:
    s = s.strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError as exc:
        raise ConfigError("config-invalid", f"{key} must be integer (got {s!r})") from exc


def load(checkout_root: Path) -> DaemonConfig:
    env_path = checkout_root / ".env"

    if not env_path.exists():
        raise ConfigError(
            "config-not-found",
            f".env not found at {env_path} — copy templates/.env.example and edit",
        )
    env = parse_env(env_path.read_text())

    # The .pr-review.yaml layer was parsed but never read, and #199 removed it.
    # A leftover file must not fail the load, but ignoring it silently would
    # recreate the original defect (editing it looks like it works), so say so.
    if (checkout_root / ".pr-review.yaml").exists():
        print(
            "load_config: ignoring .pr-review.yaml — its keys were never read "
            "and the file layer was removed (#199); delete the file",
            file=sys.stderr,
        )

    repos_raw = env.get("REPOS", "").strip()
    data = {
        "repos": repos_raw.split() if repos_raw else [],
        "github_app_id": env.get("GITHUB_APP_ID", "").strip(),
        "poll_interval_seconds": _parse_int(
            env.get("POLL_INTERVAL_SECONDS", ""), key="POLL_INTERVAL_SECONDS", default=300
        ),
        "max_parallel": _parse_int(env.get("MAX_PARALLEL", ""), key="MAX_PARALLEL", default=1),
        "opt_out_label": env.get("OPT_OUT_LABEL", "").strip() or "no-ai-review",
    }
    try:
        return DaemonConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError("config-invalid", str(exc)) from exc


def main() -> int:
    if len(sys.argv) > 1:
        checkout_root = Path(sys.argv[1]).resolve()
    else:
        checkout_root = Path(__file__).resolve().parent.parent
    try:
        config = load(checkout_root)
    except ConfigError as exc:
        print(f"category={exc.category}", file=sys.stderr)
        print(f"load_config: {exc}", file=sys.stderr)
        return 1
    print(config.model_dump_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
