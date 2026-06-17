#!/usr/bin/env python3
"""Read .env + .pr-review.yaml from the checkout root, validate, emit unified JSON.

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

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

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


class ReviewConfig(BaseModel):
    """Applied to every watched repo in V1. V2 may add a `repos:` override layer."""

    language: str = "en"
    profile: str = "chill"
    agents: list[str] = Field(default_factory=lambda: ["default"])
    path_filters: list[str] = Field(default_factory=list)
    path_instructions: list[dict] = Field(default_factory=list)
    instructions: str = ""
    max_findings: int = 10


class DaemonConfig(BaseModel):
    repos: list[str]
    github_user: str
    poll_interval_seconds: int = 300
    max_parallel: int = 1
    review_own_prs: bool = True
    opt_out_label: str = "no-ai-review"
    slack_webhook_url: str = ""
    review: ReviewConfig = Field(default_factory=ReviewConfig)

    @field_validator("repos")
    @classmethod
    def _check_repos(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("REPOS is empty — at least one owner/repo required")
        for r in v:
            if not REPO_RE.match(r):
                raise ValueError(f"invalid REPOS entry {r!r} — expected owner/repo")
        return v

    @field_validator("github_user")
    @classmethod
    def _check_user(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("GITHUB_USER is empty")
        return v

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


def _parse_bool(s: str, *, key: str) -> bool:
    s_low = s.strip().lower()
    if s_low in ("true", "1", "yes"):
        return True
    if s_low in ("false", "0", "no", ""):
        return False
    raise ConfigError("config-invalid", f"{key} must be true/false (got {s!r})")


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
    yaml_path = checkout_root / ".pr-review.yaml"

    if not env_path.exists():
        raise ConfigError(
            "config-not-found",
            f".env not found at {env_path} — copy templates/.env.example and edit",
        )
    env = parse_env(env_path.read_text())

    # .pr-review.yaml is optional — review settings fall back to schema defaults
    # if absent. Keeps minimum setup to one file edit.
    review_raw: dict = {}
    if yaml_path.exists():
        try:
            review_raw = yaml.safe_load(yaml_path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError("config-parse-error", f".pr-review.yaml: {exc}") from exc
        if not isinstance(review_raw, dict):
            raise ConfigError(
                "config-invalid",
                f".pr-review.yaml top-level must be a mapping, got {type(review_raw).__name__}",
            )

    repos_raw = env.get("REPOS", "").strip()
    data = {
        "repos": repos_raw.split() if repos_raw else [],
        "github_user": env.get("GITHUB_USER", "").strip(),
        "poll_interval_seconds": _parse_int(
            env.get("POLL_INTERVAL_SECONDS", ""), key="POLL_INTERVAL_SECONDS", default=300
        ),
        "max_parallel": _parse_int(env.get("MAX_PARALLEL", ""), key="MAX_PARALLEL", default=1),
        "review_own_prs": _parse_bool(env.get("REVIEW_OWN_PRS", "true"), key="REVIEW_OWN_PRS"),
        "opt_out_label": env.get("OPT_OUT_LABEL", "").strip() or "no-ai-review",
        "slack_webhook_url": env.get("SLACK_WEBHOOK_URL", "").strip(),
        "review": review_raw,
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
